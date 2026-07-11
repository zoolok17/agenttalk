"""Tests for the lane deliver-gate (middle-tier Phase 1).

Two layers, mirroring gates/close:

* PURE verdict tests — `compute_verdict` over synthetic resolved inputs (segment
  bounds, disjointness, one per stable hold code). No git, no I/O.
* CLI integration — `main(argv)` against a real git repo whose `.agenttalk/` store
  is gitignored (so it never pollutes base..head), exercising the git adapter
  (name-status -z parse, merge-tree), the assign overlap lock, and the deliver
  artifact-before-clear contract.
"""

from __future__ import annotations

import contextlib
import builtins
import json
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agenttalk import cli, gates, lanes
from agenttalk.store import Store

SHA = "a" * 40
OTHER = "b" * 40


# --------------------------------------------------------------- pure: bounds

def test_segment_bounds_not_string_prefix() -> None:
    assert lanes.path_under_prefix("src/foo/x.py", "src/foo", casefold=False) is True
    assert lanes.path_under_prefix("src/foo", "src/foo", casefold=False) is True
    # the classic string-prefix bug: src/foobar must NOT be under src/foo
    assert lanes.path_under_prefix("src/foobar/x.py", "src/foo", casefold=False) is False
    # empty prefix = whole repo
    assert lanes.path_under_prefix("anything/x", "", casefold=False) is True


def test_prefixes_disjoint() -> None:
    cf = False
    assert lanes.prefixes_disjoint(["src/foo"], ["src/bar"], casefold=cf) is True
    assert lanes.prefixes_disjoint(["src/foo"], ["src/foobar"], casefold=cf) is True
    assert lanes.prefixes_disjoint(["src/foo"], ["src/foo/sub"], casefold=cf) is False
    assert lanes.prefixes_disjoint(["src/foo"], ["src/foo"], casefold=cf) is False
    assert lanes.prefixes_disjoint(["src"], [], casefold=cf) is False   # whole-domain overlaps


# --------------------------------------------------------------- pure: verdict

def _lane(**over) -> dict:
    rec = lanes.new_lane(
        "l1", assignee="dev", assigned_by="lead", assigned_at="t", domain_id="core",
        path_subset=["src/core"], base_sha=SHA, target_ref="main",
        target_head_at_assign=OTHER, epoch_at_assign=None, registry_hash_at_assign="rh1",
        instance_id="1" * 32)
    rec.update(over)
    return rec


def _changed(*paths, error=None):
    return {"error": error, "paths": [
        {"path": p, "status": "M", "touched": True} for p in paths]}


def _cls(domains, shared=None, *, approvers=None, leads=None):
    cls = {"domains": domains, "shared_paths": shared or [], "unowned": not domains}
    if shared:
        # Mirror the CLI: map each matching shared entry glob to its authorized
        # approver set (close leads + the entry's default_approvers). The pure
        # verdict reads these resolved fields; it never resolves refsets itself.
        per_glob = {s["glob"]: sorted(set(approvers or []) | set(leads or []))
                    for s in shared}
        cls["shared_entry_approvers"] = per_glob
        cls["close_leads"] = sorted(leads or [])
    return cls


def _cls_multi(per_glob_approvers: dict, *, leads=None):
    """A shared classification with MULTIPLE overlapping entries: {glob: [approvers]}.
    Mirrors the CLI enrichment (each entry's approvers already unioned with leads)."""
    leads = leads or []
    return {
        "domains": [], "unowned": True,
        "shared_paths": [{"glob": g} for g in per_glob_approvers],
        "shared_entry_approvers": {g: sorted(set(ap) | set(leads))
                                   for g, ap in per_glob_approvers.items()},
        "close_leads": sorted(leads),
    }


def _gate_go():
    return {"verdict": "GO", "blockers": [], "gates": []}


def _ev(lane, changed, classifications, **over):
    kw = {"active_lanes": [], "current_epoch": None, "current_registry_hash": "rh1",
          "merge": {"status": "clean"}, "gate_check": _gate_go(), "casefold": False}
    kw.update(over)
    return lanes.compute_verdict(lane, changed=changed, classifications=classifications, **kw)


def _codes(v):
    return {h["code"] for h in v["holds"]}


def test_lane_go_when_in_bounds_clean_gate_go() -> None:
    v = _ev(_lane(), _changed("src/core/a.py"), {"src/core/a.py": _cls(["core"])})
    assert v["verdict"] == lanes.VERDICT_GO and v["holds"] == []


def test_hold_malformed() -> None:
    assert lanes.HOLD_MALFORMED in _codes(_ev({"nope": 1}, _changed(), {}))


def test_hold_stale_epoch_and_registry() -> None:
    v = _ev(_lane(), _changed("src/core/a.py"), {"src/core/a.py": _cls(["core"])},
            current_epoch="E2", current_registry_hash="rh2")
    assert lanes.HOLD_STALE_EPOCH in _codes(v)
    assert lanes.HOLD_STALE_REGISTRY in _codes(v)


def test_hold_diff_unavailable_and_parse_error() -> None:
    assert lanes.HOLD_DIFF_UNAVAILABLE in _codes(
        _ev(_lane(), {"error": "unavailable", "paths": []}, {}))
    assert lanes.HOLD_DIFF_PARSE_ERROR in _codes(
        _ev(_lane(), {"error": "parse_error", "paths": []}, {}))


def test_hold_out_of_bounds_subset() -> None:
    v = _ev(_lane(), _changed("docs/x.md"), {"docs/x.md": _cls(["docs"])})
    assert lanes.HOLD_OUT_OF_BOUNDS in _codes(v)


def test_hold_out_of_bounds_other_domain() -> None:
    # in the subset path-wise, but owned by a different domain
    v = _ev(_lane(path_subset=["src"]), _changed("src/x.py"), {"src/x.py": _cls(["other"])})
    assert lanes.HOLD_OUT_OF_BOUNDS in _codes(v)


def test_hold_unowned() -> None:
    v = _ev(_lane(path_subset=["src"]), _changed("src/x.py"), {"src/x.py": _cls([])})
    assert lanes.HOLD_UNOWNED in _codes(v)


def test_hold_domain_overlap() -> None:
    v = _ev(_lane(path_subset=["src"]), _changed("src/x.py"),
            {"src/x.py": _cls(["core", "other"])})
    assert lanes.HOLD_DOMAIN_OVERLAP in _codes(v)


def test_hold_shared_missing_then_approved() -> None:
    lane = _lane(path_subset=["shared"])
    changed = _changed("shared/x.py")
    cls = {"shared/x.py": _cls([], shared=[{"glob": "shared/**"}],
                               approvers=["dev2"], leads=["lead"])}
    assert lanes.HOLD_SHARED_MISSING_APPROVAL in _codes(_ev(lane, changed, cls))
    # an approval on the MATCHED ENTRY GLOB by an authorized approver, fresh -> GO
    lanes.add_shared_approval(lane, path_or_glob="shared/**", approved_by="dev2",
                              reason="ok", at="t", epoch=None, registry_hash="rh1")
    assert _ev(lane, changed, cls)["verdict"] == lanes.VERDICT_GO


# --- C2 (0.40.0): shared-approval over-grant + verdict-time revalidation ------

def test_shared_broad_prefix_token_does_not_clear_nested() -> None:
    """AUDIT over-grant fix: the dropped path_under_prefix arm means a broad raw
    segment-prefix token ('shared') no longer clears a nested shared path it does
    not glob-match. The approval simply does not match -> still MISSING."""
    lane = _lane(path_subset=["shared"])
    changed = _changed("shared/secret.sql")
    cls = {"shared/secret.sql": _cls([], shared=[{"glob": "shared/secret.sql"}],
                                     approvers=["dba"], leads=["lead"])}
    lanes.add_shared_approval(lane, path_or_glob="shared", approved_by="lead",
                              reason="broad", at="t", epoch=None, registry_hash="rh1")
    v = _ev(lane, changed, cls)
    assert lanes.HOLD_SHARED_MISSING_APPROVAL in _codes(v)
    assert v["verdict"] == lanes.VERDICT_HOLD


def test_shared_forged_approver_is_wrong_not_ok() -> None:
    """An approval that MATCHES the path but was recorded by someone NOT authorized
    for the matched entry -> HOLD_SHARED_WRONG_APPROVAL (the dead code goes live),
    NOT a clear and NOT a plain 'missing'."""
    lane = _lane(path_subset=["shared"])
    changed = _changed("shared/x.py")
    cls = {"shared/x.py": _cls([], shared=[{"glob": "shared/**"}],
                               approvers=["dev2"], leads=["lead"])}
    lanes.add_shared_approval(lane, path_or_glob="shared/**", approved_by="intruder",
                              reason="forged", at="t", epoch=None, registry_hash="rh1")
    v = _ev(lane, changed, cls)
    codes = _codes(v)
    assert lanes.HOLD_SHARED_WRONG_APPROVAL in codes
    assert lanes.HOLD_SHARED_MISSING_APPROVAL not in codes
    assert v["verdict"] == lanes.VERDICT_HOLD


def test_shared_stale_epoch_approval_is_wrong() -> None:
    """A matching, authorized approval that is STALE (epoch moved since it was
    recorded) does not clear -> HOLD_SHARED_WRONG_APPROVAL."""
    lane = _lane(path_subset=["shared"])
    changed = _changed("shared/x.py")
    cls = {"shared/x.py": _cls([], shared=[{"glob": "shared/**"}],
                               approvers=["dev2"], leads=["lead"])}
    lanes.add_shared_approval(lane, path_or_glob="shared/**", approved_by="dev2",
                              reason="ok", at="t", epoch="E1", registry_hash="rh1")
    v = _ev(lane, changed, cls, current_epoch="E2")  # epoch moved
    assert lanes.HOLD_SHARED_WRONG_APPROVAL in _codes(v)


def test_shared_stale_registry_approval_is_wrong() -> None:
    lane = _lane(path_subset=["shared"])
    changed = _changed("shared/x.py")
    cls = {"shared/x.py": _cls([], shared=[{"glob": "shared/**"}],
                               approvers=["dev2"], leads=["lead"])}
    lanes.add_shared_approval(lane, path_or_glob="shared/**", approved_by="dev2",
                              reason="ok", at="t", epoch=None, registry_hash="rh1")
    v = _ev(lane, changed, cls, current_registry_hash="rh2")  # registry changed
    assert lanes.HOLD_SHARED_WRONG_APPROVAL in _codes(v)


def test_shared_lead_always_authorized() -> None:
    """A close lead is authorized for any matched shared entry (covers a registry
    that later dropped the entry the approval named)."""
    lane = _lane(path_subset=["shared"])
    changed = _changed("shared/x.py")
    cls = {"shared/x.py": _cls([], shared=[{"glob": "shared/**"}],
                               approvers=["dev2"], leads=["lead"])}
    lanes.add_shared_approval(lane, path_or_glob="shared/**", approved_by="lead",
                              reason="lead ok", at="t", epoch=None, registry_hash="rh1")
    assert _ev(lane, changed, cls)["verdict"] == lanes.VERDICT_GO


# --- C2 (0.40.0) D-11: ALL-matching-entries-must-approve (overlapping shared entries) -

def _approve(lane, glob, who, *, epoch=None, rh="rh1"):
    lanes.add_shared_approval(lane, path_or_glob=glob, approved_by=who, reason="ok",
                              at="t", epoch=epoch, registry_hash=rh)


def test_shared_overlap_needs_all_matching_entries() -> None:
    # shared/** (A) AND shared/secret.sql (B) both match shared/secret.sql -> BOTH must
    # approve. Neither alone clears (one approval each -> the other entry still missing).
    changed = _changed("shared/secret.sql")
    cls = {"shared/secret.sql": _cls_multi(
        {"shared/**": ["A"], "shared/secret.sql": ["B"]}, leads=["lead"])}
    only_a = _lane(path_subset=["shared"])
    _approve(only_a, "shared/**", "A")
    assert lanes.HOLD_SHARED_MISSING_APPROVAL in _codes(_ev(only_a, changed, cls))
    only_b = _lane(path_subset=["shared"])
    _approve(only_b, "shared/secret.sql", "B")
    assert lanes.HOLD_SHARED_MISSING_APPROVAL in _codes(_ev(only_b, changed, cls))
    both = _lane(path_subset=["shared"])
    _approve(both, "shared/**", "A")
    _approve(both, "shared/secret.sql", "B")
    assert _ev(both, changed, cls)["verdict"] == lanes.VERDICT_GO


def test_shared_incomparable_overlap_needs_both() -> None:
    # reviewer-1 + codex P1 (the bug that rejected 48825db): NON-comparable overlapping
    # globs - shared/a/** (B) and shared/*/b.sql (A) both match shared/a/b.sql, neither a
    # subset of the other. Under all-matching, BOTH A and B must approve; neither alone
    # clears (no winner-picking -> no bypass).
    changed = _changed("shared/a/b.sql")
    cls = {"shared/a/b.sql": _cls_multi(
        {"shared/a/**": ["B"], "shared/*/b.sql": ["A"]}, leads=["lead"])}
    only_a = _lane(path_subset=["shared"])
    _approve(only_a, "shared/*/b.sql", "A")
    assert lanes.HOLD_SHARED_MISSING_APPROVAL in _codes(_ev(only_a, changed, cls))
    only_b = _lane(path_subset=["shared"])
    _approve(only_b, "shared/a/**", "B")
    assert lanes.HOLD_SHARED_MISSING_APPROVAL in _codes(_ev(only_b, changed, cls))
    both = _lane(path_subset=["shared"])
    _approve(both, "shared/a/**", "B")
    _approve(both, "shared/*/b.sql", "A")
    assert _ev(both, changed, cls)["verdict"] == lanes.VERDICT_GO


def test_shared_overlap_lead_clears_all_entries() -> None:
    # a close lead is authorized for EVERY matching entry, so one approval per entry by
    # the lead clears the path (the CLI records all of them in one approve-shared call).
    changed = _changed("shared/secret.sql")
    cls = {"shared/secret.sql": _cls_multi(
        {"shared/**": ["A"], "shared/secret.sql": ["B"]}, leads=["lead"])}
    lane = _lane(path_subset=["shared"])
    _approve(lane, "shared/**", "lead")
    _approve(lane, "shared/secret.sql", "lead")
    assert _ev(lane, changed, cls)["verdict"] == lanes.VERDICT_GO


def test_shared_overlap_unauthorized_against_matching_entry_is_wrong() -> None:
    # An approval recorded against a matching entry by someone NOT authorized for it is
    # "wrong" (more actionable than missing).
    changed = _changed("shared/secret.sql")
    cls = {"shared/secret.sql": _cls_multi(
        {"shared/**": ["A"], "shared/secret.sql": ["B"]}, leads=["lead"])}
    lane = _lane(path_subset=["shared"])
    _approve(lane, "shared/**", "A")                       # shared/** satisfied
    _approve(lane, "shared/secret.sql", "A")               # A not authorized for secret.sql
    assert lanes.HOLD_SHARED_WRONG_APPROVAL in _codes(_ev(lane, changed, cls))


def test_hold_active_lane_overlap() -> None:
    other = _lane(lane_id="l2", path_subset=["src/core/sub"])
    v = _ev(_lane(), _changed("src/core/sub/x.py"), {"src/core/sub/x.py": _cls(["core"])},
            active_lanes=[other])
    assert lanes.HOLD_ACTIVE_LANE_OVERLAP in _codes(v)


def test_active_lane_overlap_is_domain_aware() -> None:
    # reviewer-1 MAJOR: a whole-domain lane in ANOTHER domain must NOT false-overlap
    # (path_in_subset([]) is "all paths", so without the domain filter it would).
    other = _lane(lane_id="l2", domain_id="other", path_subset=[])  # whole 'other' domain
    v = _ev(_lane(), _changed("src/core/a.py"), {"src/core/a.py": _cls(["core"])},
            active_lanes=[other])
    assert lanes.HOLD_ACTIVE_LANE_OVERLAP not in _codes(v)
    assert v["verdict"] == lanes.VERDICT_GO
    # a SAME-domain whole-domain lane still overlaps
    same = _lane(lane_id="l3", domain_id="core", path_subset=[])
    v2 = _ev(_lane(), _changed("src/core/a.py"), {"src/core/a.py": _cls(["core"])},
             active_lanes=[same])
    assert lanes.HOLD_ACTIVE_LANE_OVERLAP in _codes(v2)


def test_hold_merge_conflict_and_unknown() -> None:
    base = (_lane(), _changed("src/core/a.py"), {"src/core/a.py": _cls(["core"])})
    assert lanes.HOLD_MERGE_CONFLICT in _codes(
        _ev(*base, merge={"status": "conflict", "detail": "x"}))
    assert lanes.HOLD_MERGE_UNKNOWN in _codes(
        _ev(*base, merge={"status": "unknown", "detail": "git<2.38"}))


def test_hold_gate() -> None:
    v = _ev(_lane(), _changed("src/core/a.py"), {"src/core/a.py": _cls(["core"])},
            gate_check={"verdict": "HOLD", "blockers": [{"name": "ci"}]})
    assert lanes.HOLD_GATE in _codes(v)


def test_hold_casefold_collision() -> None:
    changed = {"error": None, "paths": [
        {"path": "src/core/A.py", "status": "M", "touched": True},
        {"path": "src/core/a.py", "status": "A", "touched": True}]}
    v = _ev(_lane(), changed, {"src/core/A.py": _cls(["core"]), "src/core/a.py": _cls(["core"])})
    assert lanes.HOLD_CASEFOLD_COLLISION in _codes(v)


def test_copy_source_is_evidence_only() -> None:
    # a copy SOURCE outside bounds must NOT trip out_of_bounds (it isn't written)
    changed = {"error": None, "paths": [
        {"path": "outside/orig.py", "status": "C75", "touched": False, "role": "copy-source"},
        {"path": "src/core/copy.py", "old_path": "outside/orig.py", "status": "C75",
         "touched": True, "role": "copy-dest"}]}
    cls = {"src/core/copy.py": _cls(["core"])}
    assert _ev(_lane(), changed, cls)["verdict"] == lanes.VERDICT_GO


def test_rename_old_path_is_in_bounds_checked() -> None:
    # a rename whose OLD path is out of bounds must HOLD (the old path is removed)
    changed = {"error": None, "paths": [
        {"path": "outside/old.py", "status": "R100", "touched": True, "role": "rename-old"},
        {"path": "src/core/new.py", "old_path": "outside/old.py", "status": "R100",
         "touched": True, "role": "rename-new"}]}
    cls = {"outside/old.py": _cls(["other"]), "src/core/new.py": _cls(["core"])}
    assert lanes.HOLD_OUT_OF_BOUNDS in _codes(_ev(_lane(path_subset=["src/core", "outside"]),
                                                  changed, cls))


# --------------------------------------------------------------- CLI integration

def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, encoding="utf-8").stdout


def _git_rc(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, encoding="utf-8")


def _repo(tmp_path: Path) -> tuple[Path, str]:
    """A git repo with .agenttalk gitignored + an initialized store + a core domain."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".gitignore").write_text(".agenttalk/\n.worktrees/\n", encoding="utf-8")
    (tmp_path / "src" / "core").mkdir(parents=True)
    (tmp_path / "src" / "other").mkdir(parents=True)
    (tmp_path / "src" / "core" / "a.py").write_text("base\n", encoding="utf-8")
    (tmp_path / "src" / "other" / "b.py").write_text("o\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD").strip()
    s = Store(tmp_path)
    s.init(["lead", "dev", "dev2"])
    s.set_role("lead", "lead")
    (s.dir / "domains.json").write_text(json.dumps({
        "schema_version": 1, "domains": {"core": {
            "title": "Core", "owners": {"agents": ["dev"]},
            "owned_globs": ["src/core/**"]}}, "shared_paths": []}), encoding="utf-8")
    return tmp_path, base


def _run(argv: list[str], root: Path) -> int:
    if argv[:2] == ["lane", "assign"] and "--no-worktree" not in argv and "--worktrees-root" not in argv:
        argv = [*argv, "--advisory", "--no-worktree",
                "--worktree-waiver-reason", "advisory deliver-gate test"]
    return cli.main(["--root", str(root), *argv])


def _run_raw(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _branch(root: Path) -> str:
    return _git(root, "branch", "--show-current").strip()


def _commit(root: Path, rel: str, text: str) -> str:
    (root / rel).write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")
    return _git(root, "rev-parse", "HEAD").strip()


def test_cli_assign_check_deliver_go(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    br = _branch(root)
    assert _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
                 "--domain", "core", "--base", base, "--target", br, "--path", "src/core"], root) == 0
    head = _commit(root, "src/core/a.py", "base\nchange\n")
    assert _run(["lane", "check", "--id", "l1", "--head", head], root) == 0       # in-bounds GO
    assert _run(["lane", "deliver", "--id", "l1", "--from", "dev", "--head", head], root) == 0
    # GO cleared the lane and wrote a durable artifact
    assert lanes.active_lanes(lanes.load_lanes(Store(root))) == []
    arts = list((Store(root).dir / "lane-deliveries").glob("l1-*.json"))
    assert len(arts) == 1
    artifact = json.loads(arts[0].read_text(encoding="utf-8"))
    assert artifact["verdict"] == "GO"
    assert artifact["integrity_version"] == 3
    assert artifact["artifact_state"] == lanes.ARTIFACT_COMMITTED
    snapshot = artifact["evaluation_snapshot"]
    assert snapshot["candidate_head"] == head
    assert snapshot["target_head"] == head
    assert snapshot["current_registry_hash"]
    assert snapshot["active_lane_fingerprints"] == []
    for key in (
        "lane_fingerprint", "gate_digest", "changed_digest",
        "classifications_digest", "merge_digest", "worktree_digest",
        "config_digest", "cooperating_digest",
    ):
        assert re.fullmatch(r"[0-9a-f]{64}", snapshot[key])
    snapshot["gate_digest"] = "0" * 64
    arts[0].write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(lanes.LaneError, match="integrity token"):
        lanes.validate_delivery_artifact(
            arts[0], lane_id="l1", head_sha=head, store=Store(root),
            require_isolation=True,
        )


def test_cli_check_out_of_bounds_holds(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    br = _branch(root)
    _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
          "--domain", "core", "--base", base, "--target", br, "--path", "src/core"], root)
    head = _commit(root, "src/other/b.py", "o\nstray\n")   # outside src/core
    assert _run(["lane", "check", "--id", "l1", "--head", head], root) == 3


def test_cli_deliver_recomputes_gate_after_entering_final_lock(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The old path evaluated GO before taking the final lock. Inject a blocker as
    # the lock is entered: delivery must recompute there and leave no GO artifact.
    root, base = _repo(tmp_path)
    br = _branch(root)
    _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
          "--domain", "core", "--base", base, "--target", br, "--path", "src/core"], root)
    head = _commit(root, "src/core/a.py", "base\nchange\n")
    real_lock = Store._config_lock

    @contextlib.contextmanager
    def lock_with_new_blocker(self, *args, **kwargs):  # noqa: ANN001
        with real_lock(self, *args, **kwargs):
            gates.set_gate(
                root, name="late-ci", status="red", severity="blocker",
                scope="lane:l1", actor="lead", evidence_source="local_command",
                reason="landed after the obsolete pre-lock evaluation", required=True,
            )
            yield

    monkeypatch.setattr(Store, "_config_lock", lock_with_new_blocker)
    assert _run(["lane", "deliver", "--id", "l1", "--from", "dev", "--head", head], root) == 3
    deliveries = Store(root).dir / "lane-deliveries"
    assert not deliveries.exists() or not list(deliveries.glob("*.json"))
    assert [ln["lane_id"] for ln in lanes.active_lanes(lanes.load_lanes(Store(root)))] == ["l1"]


def test_cli_deliver_hold_leaves_lane_active(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    br = _branch(root)
    _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
          "--domain", "core", "--base", base, "--target", br, "--path", "src/core"], root)
    head = _commit(root, "src/other/b.py", "o\nstray\n")
    assert _run(["lane", "deliver", "--id", "l1", "--from", "dev", "--head", head], root) == 3
    assert [ln["lane_id"] for ln in lanes.active_lanes(lanes.load_lanes(Store(root)))] == ["l1"]
    assert not (Store(root).dir / "lane-deliveries").exists() or \
        not list((Store(root).dir / "lane-deliveries").glob("*.json"))


def test_whole_domain_lane_can_go(tmp_path: Path) -> None:
    # codex blocker 1: an empty path_subset (whole domain) must GO on in-domain paths,
    # not flag every path out_of_bounds.
    root, base = _repo(tmp_path)
    br = _branch(root)
    assert _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
                 "--domain", "core", "--base", base, "--target", br], root) == 0  # no --path
    head = _commit(root, "src/core/a.py", "base\nchange\n")
    assert _run(["lane", "check", "--id", "l1", "--head", head], root) == 0


def test_pure_whole_domain_in_subset() -> None:
    assert lanes.path_in_subset("src/core/a.py", [], casefold=False) is True
    v = _ev(_lane(path_subset=[]), _changed("src/core/a.py"), {"src/core/a.py": _cls(["core"])})
    assert v["verdict"] == lanes.VERDICT_GO


def test_cli_whole_domain_lanes_different_domains_both_assign(tmp_path: Path) -> None:
    # codex blocker 1: two whole-domain lanes in DIFFERENT domains must not be refused.
    root, base = _repo(tmp_path)
    br = _branch(root)
    s = Store(root)
    (s.dir / "domains.json").write_text(json.dumps({
        "schema_version": 1, "domains": {
            "core": {"title": "Core", "owners": {"agents": ["dev"]}, "owned_globs": ["src/core/**"]},
            "other": {"title": "Other", "owners": {"agents": ["dev2"]}, "owned_globs": ["src/other/**"]}},
        "shared_paths": []}), encoding="utf-8")
    assert _run(["lane", "assign", "--id", "lc", "--from", "lead", "--assignee", "dev",
                 "--domain", "core", "--base", base, "--target", br], root) == 0
    assert _run(["lane", "assign", "--id", "lo", "--from", "lead", "--assignee", "dev2",
                 "--domain", "other", "--base", base, "--target", br], root) == 0


def test_cli_assign_overlap_refused(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    br = _branch(root)
    assert _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
                 "--domain", "core", "--base", base, "--target", br, "--path", "src/core"], root) == 0
    # overlapping subset on a second lane is refused (fail closed)
    assert _run(["lane", "assign", "--id", "l2", "--from", "lead", "--assignee", "dev2",
                 "--domain", "core", "--base", base, "--target", br, "--path", "src/core/sub"], root) == 2


def test_lane_fingerprint_distinguishes_reassign() -> None:
    # codex blocker 3: a reassigned lane (new base/target/assigned_at) has a different
    # fingerprint, so deliver will not clear a lane it did not evaluate.
    a = _lane()
    b = _lane(base_sha="c" * 40)
    c = _lane(assigned_at="later")
    d = _lane(shared_approvals=[{
        "path_or_glob": "shared/**", "approved_by": "lead", "reason": "ok",
        "at": "t", "epoch": None, "registry_hash": "rh1",
    }])
    e = _lane(epoch_at_assign="epoch-2")
    f = _lane(worktree_waived=True, worktree_waived_by="dev",
              worktree_waiver_reason="forged", worktree_waived_at="t")
    g = _lane(instance_id="2" * 32)
    h = _lane(generation=2)
    assert lanes.fingerprint(a) != lanes.fingerprint(b)
    assert lanes.fingerprint(a) != lanes.fingerprint(c)
    assert lanes.fingerprint(a) != lanes.fingerprint(d)
    assert lanes.fingerprint(a) != lanes.fingerprint(e)
    assert lanes.fingerprint(a) != lanes.fingerprint(f)
    assert lanes.fingerprint(a) != lanes.fingerprint(g)
    assert lanes.fingerprint(a) != lanes.fingerprint(h)
    assert lanes.fingerprint(a) == lanes.fingerprint(dict(a))


def test_cli_release_lane_refuses_no_worktree_and_advisory_is_never_release_evidence(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    br = _branch(root)
    common = ["--domain", "core", "--base", base, "--target", br,
              "--path", "src/core", "--no-worktree",
              "--worktree-waiver-reason", "explicit exception"]

    assert _run_raw([
        "lane", "assign", "--id", "denied", "--from", "lead",
        "--assignee", "dev", *common,
    ], root) == 2
    assert "release-class" in capsys.readouterr().err.lower()
    assert "denied" not in lanes.load_lanes(Store(root))["lanes"]

    assert _run_raw([
        "lane", "assign", "--id", "advisory", "--from", "dev",
        "--assignee", "dev", "--advisory", *common,
    ], root) == 0
    head = _commit(root, "src/core/a.py", "base\nadvisory\n")
    assert _run_raw([
        "lane", "deliver", "--id", "advisory", "--from", "dev", "--head", head,
    ], root) == 0
    artifact = next((Store(root).dir / "lane-deliveries").glob("advisory-*.json"))
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["isolation_status"] == "advisory_unisolated"
    with pytest.raises(lanes.LaneError, match="isolation"):
        lanes.validate_delivery_artifact(
            artifact, lane_id="advisory", head_sha=head, store=Store(root),
            require_isolation=True,
        )
    assert _run_raw([
        "close", "open", "--id", "advisory-close", "--from", "lead",
        "--scope", "release", "--revision", head,
        "--lane-artifact", str(artifact),
    ], root) == 0
    assert _run_raw(["close", "check", "--id", "advisory-close"], root) == 3

    legacy = dict(payload)
    legacy["integrity_version"] = lanes.LEGACY_INTEGRITY_VERSION
    legacy["evaluation_snapshot"] = dict(legacy["evaluation_snapshot"])
    legacy["evaluation_snapshot"]["evaluation_version"] = lanes.LEGACY_EVALUATION_VERSION
    legacy["isolation_status"] = "waived"
    legacy["worktree_waiver_authority"] = "sole_lead"
    legacy["integrity_token"] = lanes.compute_integrity_token(Store(root), legacy)
    legacy_path = Store(root).dir / "lane-deliveries" / "legacy-waived.json"
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
    with pytest.raises(lanes.LaneError, match="legacy waived"):
        lanes.validate_delivery_artifact(
            legacy_path, lane_id="advisory", head_sha=head, store=Store(root),
            require_isolation=True,
        )

    assert _run_raw([
        "lane", "assign", "--id", "advisory-isolated", "--from", "dev",
        "--assignee", "dev", "--advisory", "--domain", "core", "--base", head,
        "--target", br, "--path", "src/core",
    ], root) == 0
    isolated_lane = lanes.load_lanes(Store(root))["lanes"]["advisory-isolated"]
    isolated_root = Path(isolated_lane["worktree_path"])
    isolated_head = _commit(isolated_root, "src/core/b.py", "advisory isolated\n")
    assert _run_raw([
        "lane", "deliver", "--id", "advisory-isolated", "--from", "dev",
        "--head", isolated_head,
    ], root) == 0
    isolated_artifact = next(
        (Store(root).dir / "lane-deliveries").glob("advisory-isolated-*.json")
    )
    with pytest.raises(lanes.LaneError, match="non-release advisory"):
        lanes.validate_delivery_artifact(
            isolated_artifact, lane_id="advisory-isolated", head_sha=isolated_head,
            store=Store(root), require_isolation=True,
        )
    assert _run_raw([
        "close", "open", "--id", "advisory-isolated-close", "--from", "lead",
        "--scope", "release", "--revision", isolated_head,
        "--lane-artifact", str(isolated_artifact),
    ], root) == 0
    assert _run_raw(["close", "check", "--id", "advisory-isolated-close"], root) == 3


def test_cli_deliver_rejects_forged_no_worktree_waiver(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    br = _branch(root)
    assert _run([
        "lane", "assign", "--id", "forged", "--from", "lead",
        "--assignee", "dev", "--domain", "core", "--base", base,
        "--target", br, "--path", "src/core",
    ], root) == 0
    store = Store(root)
    state = lanes.load_lanes(store)
    state["lanes"]["forged"]["release_class"] = True
    state["lanes"]["forged"]["worktree_waived_by"] = "lead"
    state["lanes"]["forged"]["worktree_waiver_authority"] = "sole_lead"
    lanes.save_lanes(store, state)
    head = _commit(root, "src/core/a.py", "base\nchange\n")

    assert _run_raw([
        "lane", "deliver", "--id", "forged", "--from", "dev", "--head", head,
    ], root) == 3
    assert lanes.load_lanes(store)["lanes"]["forged"]["status"] == lanes.STATUS_ACTIVE
    deliveries = store.dir / "lane-deliveries"
    assert not deliveries.exists() or not list(deliveries.glob("*.json"))


def test_cli_shared_approval_authority_and_fail_closed(tmp_path: Path) -> None:
    # codex blocker 2: approve-shared uses default_approvers and fails closed.
    root, base = _repo(tmp_path)
    br = _branch(root)
    s = Store(root)
    (s.dir / "domains.json").write_text(json.dumps({
        "schema_version": 1,
        "domains": {"core": {"title": "Core", "owners": {"agents": ["dev"]},
                             "owned_globs": ["src/core/**"]}},
        "shared_paths": [{"glob": "shared/**", "category": "schema",
                          "requires": "lead-approval",
                          "default_reviewers": {"agents": ["dev2"]},
                          "default_approvers": {"agents": ["dev2"]}}]}), encoding="utf-8")
    (root / "shared").mkdir()
    _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
          "--domain", "core", "--base", base, "--target", br, "--path", "shared"], root)
    # a non-approver (dev) is refused
    assert _run(["lane", "approve-shared", "--id", "l1", "--path", "shared/x",
                 "--from", "dev", "--reason", "no"], root) == 2
    # the default_approver (dev2) is allowed
    assert _run(["lane", "approve-shared", "--id", "l1", "--path", "shared/x",
                 "--from", "dev2", "--reason", "ok"], root) == 0
    # a path matching no shared entry fails closed
    assert _run(["lane", "approve-shared", "--id", "l1", "--path", "src/core/x",
                 "--from", "lead", "--reason", "x"], root) == 2


def test_cli_shared_approval_all_matching_records_authorized_entries(tmp_path: Path) -> None:
    # C2 (0.40.0, D-11): approve-shared records the actor's approval against EACH matching
    # entry the actor is authorized for, persisting the entry glob (not the raw --path),
    # and reports the entries that still need approval. ALL matching entries must be
    # approved before the path clears.
    root, base = _repo(tmp_path)
    br = _branch(root)
    s = Store(root)
    (s.dir / "domains.json").write_text(json.dumps({
        "schema_version": 1,
        "domains": {"core": {"title": "Core", "owners": {"agents": ["dev"]},
                             "owned_globs": ["src/core/**"]}},
        "shared_paths": [
            {"glob": "shared/**", "category": "schema", "requires": "lead-approval",
             "default_approvers": {"agents": ["dev2"]}},
            {"glob": "shared/secret.sql", "category": "schema", "requires": "lead-approval",
             "default_approvers": {"agents": ["dev"]}}]}), encoding="utf-8")
    (root / "shared").mkdir()
    _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
          "--domain", "core", "--base", base, "--target", br, "--path", "shared"], root)
    # shared/secret.sql matches BOTH entries. dev2 is authorized for shared/** only ->
    # records shared/**, still needs shared/secret.sql (by dev).
    assert _run(["lane", "approve-shared", "--id", "l1", "--path", "shared/secret.sql",
                 "--from", "dev2", "--reason", "broad"], root) == 0
    globs = {a["path_or_glob"] for a in lanes.load_lanes(s)["lanes"]["l1"]["shared_approvals"]}
    assert globs == {"shared/**"}
    # dev is authorized for shared/secret.sql -> records it; now BOTH entries approved.
    assert _run(["lane", "approve-shared", "--id", "l1", "--path", "shared/secret.sql",
                 "--from", "dev", "--reason", "dba ok"], root) == 0
    globs = {a["path_or_glob"] for a in lanes.load_lanes(s)["lanes"]["l1"]["shared_approvals"]}
    assert globs == {"shared/**", "shared/secret.sql"}
    # a close lead is authorized for EVERY matching entry -> one call records both.
    # Re-assign l1 fresh (--force) so its approvals reset, then lead approves once.
    _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev", "--force",
          "--domain", "core", "--base", base, "--target", br, "--path", "shared"], root)
    assert _run(["lane", "approve-shared", "--id", "l1", "--path", "shared/secret.sql",
                 "--from", "lead", "--reason", "lead clears all"], root) == 0
    lead_globs = {a["path_or_glob"] for a in lanes.load_lanes(s)["lanes"]["l1"]["shared_approvals"]}
    assert lead_globs == {"shared/**", "shared/secret.sql"}


def test_cli_assign_unknown_domain_refused(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    br = _branch(root)
    assert _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
                 "--domain", "ghost", "--base", base, "--target", br], root) == 2


def test_cli_status_lists_active(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    br = _branch(root)
    _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
          "--domain", "core", "--base", base, "--target", br, "--path", "src/core"], root)
    capsys.readouterr()
    assert _run(["lane", "status"], root) == 0
    assert "l1" in capsys.readouterr().out


def test_cli_missing_lanes_file_is_empty(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root, _base = _repo(tmp_path)
    capsys.readouterr()
    assert _run(["lane", "status"], root) == 0
    assert "none" in capsys.readouterr().out


def test_cli_reset_warns_on_active_lanes(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    br = _branch(root)
    _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
          "--domain", "core", "--base", base, "--target", br, "--path", "src/core"], root)
    capsys.readouterr()
    assert _run(["reset"], root) == 0
    err = capsys.readouterr().err
    assert "ACTIVE lane" in err and "l1" in err
    # reset cleared lanes.json
    assert lanes.active_lanes(lanes.load_lanes(Store(root))) == []


def test_cli_malformed_lanes_does_not_brick_status(tmp_path: Path) -> None:
    root, _base = _repo(tmp_path)
    state = Store(root).dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    lane_path = state / "lanes.json"
    lane_path.write_text("{bad", encoding="utf-8")
    # lane status fails closed (exit 2), but an unrelated command still works
    assert _run(["lane", "status"], root) == 2
    assert _run(["status"], root) == 0
    assert _run(["reset"], root) == 2
    assert lane_path.read_text(encoding="utf-8") == "{bad"


def _assign_worktree(root: Path, base: str, lane_id: str = "lwt") -> dict:
    assert _run_raw([
        "lane", "assign", "--id", lane_id, "--from", "lead", "--assignee", "dev",
        "--domain", "core", "--base", base, "--target", _branch(root),
        "--path", "src/core", "--worktrees-root", str(root / ".worktrees"),
    ], root) == 0
    return lanes.load_lanes(Store(root))["lanes"][lane_id]


def test_m8_lane_id_strict_and_branch_derived() -> None:
    assert lanes.lane_branch("abc-_.1") == "lane/abc-_.1"
    for bad in ("..", "a..b", "bad.", "x.lock", "x/ y", "-x", "x:y", "x\\y", "x~y", "x[y"):
        with pytest.raises(lanes.LaneError):
            lanes.validate_lane_id(bad)


def test_m6_assign_provisions_worktree_by_default_and_workspace(tmp_path: Path, capsys) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base)
    wt = Path(lane["worktree_path"])
    assert wt.exists()
    assert lane["worktree_branch"] == "lane/lwt"
    assert lane["worktree_base_sha"] == base
    assert lane["worktree_toplevel_canonical"] == lanes.canonical_host_path(wt)
    assert (root / ".worktrees" / lanes.WORKTREE_MARKER_FILENAME).exists()
    capsys.readouterr()
    assert _run_raw(["lane", "workspace", "--id", "lwt", "--json"], root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["worktree_path"] == str(wt)


def test_m1_m3_canonical_paths_and_common_git_dir_match(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "canon")
    prov = cli._verify_lane_worktree(Store(root), lane, expected_base=base)
    wt = Path(lane["worktree_path"])
    assert prov["worktree_toplevel_canonical"] == lanes.canonical_host_path(wt)
    assert prov["common_git_dir_canonical"] == cli._common_git_dir(root)


def test_s_p2_verify_lane_worktree_rejects_primary_checkout_tamper(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "mainroot")
    wt = Path(lane["worktree_path"])
    assert _git_rc(root, "worktree", "remove", str(wt)).returncode == 0
    assert _git_rc(root, "checkout", "-q", lanes.lane_branch("mainroot")).returncode == 0
    lane["worktree_path"] = str(root)
    lane["worktree_toplevel_canonical"] = lanes.canonical_host_path(root)
    lane["worktree_common_git_dir_canonical"] = cli._common_git_dir(root)

    with pytest.raises(lanes.LaneError, match="primary checkout"):
        cli._verify_lane_worktree(Store(root), lane, expected_base=base)


def test_m5_git_write_worktree_add_uses_separator_and_rejects_evil_id(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    assert _run_raw(["lane", "assign", "--id=--detach", "--from", "lead",
                     "--assignee", "dev", "--domain", "core", "--base", base,
                     "--target", _branch(root)], root) == 2
    calls = []
    real = cli._git_write

    def spy(git_root, argv, **kw):
        calls.append(list(argv))
        return real(git_root, argv, **kw)

    monkeypatch.setattr(cli, "_git_write", spy)
    _assign_worktree(root, base, "sep")
    add = next(c for c in calls if c[:3] == ["worktree", "add", "-b"])
    assert add[3] == "lane/sep"
    assert add[4] == "--"
    assert re.fullmatch(r"[0-9a-f]{40}", add[6])


def test_m2_worktree_dirty_rules_and_main_head_mismatch(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "clean")
    wt = Path(lane["worktree_path"])
    _commit(wt, "src/core/a.py", "base\nwork\n")
    (wt / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    assert _run_raw(["lane", "deliver", "--id", "clean", "--from", "dev"], root) == 0
    art = next((Store(root).dir / "lane-deliveries").glob("clean-*.json"))
    assert json.loads(art.read_text(encoding="utf-8"))["isolation_status"] == "verified"

    dirty = _assign_worktree(root, base, "dirty")
    dirty_wt = Path(dirty["worktree_path"])
    (dirty_wt / "src" / "core" / "a.py").write_text("dirty but uncommitted\n", encoding="utf-8")
    assert _run_raw(["lane", "deliver", "--id", "dirty", "--from", "dev"], root) == 3

    mismatch_root = tmp_path / "mismatch-repo"
    mismatch_root.mkdir()
    root2, base2 = _repo(mismatch_root)
    mismatch = _assign_worktree(root2, base2, "mismatch")
    assert Path(mismatch["worktree_path"]).exists()
    main_head = _commit(root2, "src/core/a.py", "main checkout change\n")
    assert _run_raw(["lane", "deliver", "--id", "mismatch", "--from", "dev",
                     "--head", main_head], root2) == 3


def test_deliver_rechecks_worktree_provenance_before_writing_artifact(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "moving")
    wt = Path(lane["worktree_path"])
    _commit(wt, "src/core/a.py", "base\nwork\n")
    real_verify = cli._verify_lane_worktree
    calls = {"count": 0}

    def moving_verify(*args, **kwargs):  # noqa: ANN002,ANN003
        result = real_verify(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 2:
            result = {**result, "head": base}
        return result

    monkeypatch.setattr(cli, "_verify_lane_worktree", moving_verify)

    assert _run_raw(["lane", "deliver", "--id", "moving", "--from", "dev"], root) == 3
    saved = lanes.load_lanes(Store(root))["lanes"]["moving"]
    assert saved["status"] == lanes.STATUS_ACTIVE
    deliveries = Store(root).dir / "lane-deliveries"
    assert not deliveries.exists() or not list(deliveries.glob("*.json"))


def test_m4_m7_close_validates_artifact_after_worktree_removed(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "rel")
    wt = Path(lane["worktree_path"])
    head = _commit(wt, "src/core/a.py", "base\nrelease\n")
    assert _run_raw(["lane", "deliver", "--id", "rel", "--from", "dev"], root) == 0
    artifact = next((Store(root).dir / "lane-deliveries").glob("rel-*.json"))
    assert not wt.exists()
    assert _run_raw(["close", "open", "--id", "ship", "--from", "lead",
                     "--scope", "release", "--revision", head,
                     "--lane-artifact", str(artifact)], root) == 0
    assert _run_raw(["close", "check", "--id", "ship"], root) == 0

    bad = Store(root).dir / "lane-deliveries" / "bad-token.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["integrity_token"] = "0" * 64
    bad.write_text(json.dumps(payload), encoding="utf-8")
    assert _run_raw(["close", "open", "--id", "bad", "--from", "lead",
                     "--scope", "release", "--revision", head,
                     "--lane-artifact", str(bad)], root) == 0
    assert _run_raw(["close", "check", "--id", "bad"], root) == 3


def test_m9_active_launch_prevents_deliver_teardown(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "busy")
    wt = Path(lane["worktree_path"])
    _commit(wt, "src/core/a.py", "base\nbusy\n")
    Store(root).write_launch_request({
        "request_id": "lr-busy", "state": "queued", "lane_id": "busy",
    })
    assert _run_raw(["lane", "deliver", "--id", "busy", "--from", "dev"], root) == 0
    saved = lanes.load_lanes(Store(root))["lanes"]["busy"]
    assert saved["status"] == lanes.STATUS_DELIVERED
    assert saved["worktree_state"] == lanes.STATUS_CLEANUP_PENDING
    assert wt.exists()


def test_abandon_active_launch_defers_worktree_removal(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "busyabandon")
    wt = Path(lane["worktree_path"])
    Store(root).write_launch_request({
        "request_id": "lr-busy-abandon", "state": "queued", "lane_id": "busyabandon",
    })
    assert _run_raw(["lane", "abandon", "--id", "busyabandon"], root) == 0
    saved = lanes.load_lanes(Store(root))["lanes"]["busyabandon"]
    assert saved["status"] == lanes.STATUS_ABANDONED
    assert saved["worktree_state"] == lanes.STATUS_CLEANUP_PENDING
    assert wt.exists()


def test_abandon_delete_branch_active_launch_keeps_branch_until_cleanup(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "busybranch")
    wt = Path(lane["worktree_path"])
    store = Store(root)
    store.write_launch_request({
        "request_id": "lr-busy-branch", "state": "queued", "lane_id": "busybranch",
    })
    assert _run_raw(["lane", "abandon", "--id", "busybranch", "--delete-branch",
                     "--target", _branch(root)], root) == 0
    captured = capsys.readouterr()
    saved = lanes.load_lanes(store)["lanes"]["busybranch"]
    assert saved["status"] == lanes.STATUS_ABANDONED
    assert saved["worktree_state"] == lanes.STATUS_CLEANUP_PENDING
    assert wt.exists()
    assert "not deleted - worktree has an active or pending launch" in captured.err
    assert _git_rc(root, "rev-parse", "--verify",
                   "refs/heads/lane/busybranch^{commit}").returncode == 0

    assert store.archive_launch_request("lr-busy-branch", {
        "request_id": "lr-busy-branch", "terminal_state": "archived"})
    assert _run_raw(["lane", "gc", "--delete", "--json"], root) == 0
    payload = json.loads(capsys.readouterr().out)
    item = next(i for i in payload["items"] if i["lane_id"] == "busybranch")
    assert item["worktree_removed"] is True
    assert not wt.exists()


def test_m10_gc_discovers_lane_worktree_after_reset(tmp_path: Path, capsys) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "orphan")
    wt = Path(lane["worktree_path"])
    assert _run_raw(["reset"], root) == 0
    capsys.readouterr()
    assert _run_raw(["lane", "gc", "--json"], root) == 0
    payload = json.loads(capsys.readouterr().out)
    item = next(i for i in payload["items"] if i["lane_id"] == "orphan")
    assert lanes.canonical_host_path(item["worktree"]) == lanes.canonical_host_path(wt)
    assert item["status"] == "orphaned"


def test_s1_gc_dry_run_reports_managed_leftover_without_lane_record(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    managed = root / ".worktrees"
    managed.mkdir()
    (managed / lanes.WORKTREE_MARKER_FILENAME).write_text(
        "agenttalk managed worktrees\n", encoding="utf-8")
    leftover = managed / f"leftover-{base[:12]}-abcdef12"
    leftover.mkdir()
    capsys.readouterr()
    assert _run_raw(["lane", "gc", "--json"], root) == 0
    payload = json.loads(capsys.readouterr().out)
    item = next(i for i in payload["items"] if i["lane_id"] == "leftover")
    assert item["status"] == "orphaned"
    assert item["worktree"] == str(leftover)
    assert item["worktree_remove_safe"] is False


def test_m11_stale_epoch_denies_without_git_write(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    epoch_calls = {"n": 0}
    fp_calls = {"n": 0}

    def moving_epoch(self):  # noqa: ANN001 - monkeypatch signature follows Store
        epoch_calls["n"] += 1
        if epoch_calls["n"] > 1:
            raise AssertionError("current_epoch must stay outside the assign lock")
        return "epoch-before"

    def moving_fingerprint(_store):  # noqa: ANN001 - monkeypatch signature follows CLI helper
        fp_calls["n"] += 1
        return ("fp-before",) if fp_calls["n"] == 1 else ("fp-after",)

    def no_write(*_a, **_kw):
        raise AssertionError("_git_write must not run after a stale assignment fingerprint")

    monkeypatch.setattr(Store, "current_epoch", moving_epoch)
    monkeypatch.setattr(cli, "_lane_assignment_fingerprint", moving_fingerprint)
    monkeypatch.setattr(cli, "_git_write", no_write)
    assert _run_raw(["lane", "assign", "--id", "stale", "--from", "lead",
                     "--assignee", "dev", "--domain", "core", "--base", base,
                     "--target", _branch(root), "--worktrees-root",
                     str(root / ".worktrees")], root) == 2
    assert epoch_calls["n"] == 1


def test_m12_git_write_env_timeout_and_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = {}

    class OkProc:
        returncode = 0

        def communicate(self, timeout=None):  # noqa: ANN001
            return "", ""

    def ok_popen(cmd, **kw):  # noqa: ANN001
        seen["cmd"] = cmd
        seen["env"] = kw["env"]
        return OkProc()

    monkeypatch.setattr(cli.subprocess, "Popen", ok_popen)
    full_sha = "a" * 40
    rc, _out, _err = cli._git_write(
        tmp_path, ["worktree", "add", "-b", "lane/safe", "--", str(tmp_path / "wt"), full_sha])
    assert rc == 0
    assert seen["cmd"][:4] == ["git", "-c", "core.editor=false", "-C"]
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert "GIT_ASKPASS" not in seen["env"]
    with pytest.raises(cli.GitWriteError):
        cli._git_write(tmp_path, ["branch", "-D", "lane/safe"])
    with pytest.raises(cli.GitWriteError):
        cli._git_write(
            tmp_path, ["worktree", "add", "-b", "lane/bad/slash", "--",
                       str(tmp_path / "bad"), full_sha])

    class StuckProc:
        def communicate(self, timeout=None):  # noqa: ANN001
            raise subprocess.TimeoutExpired("git", timeout)

        def kill(self):
            seen["killed"] = True

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_kw: StuckProc())
    with pytest.raises(cli.GitWriteError, match="could not be reaped"):
        cli._git_write(
            tmp_path, ["worktree", "add", "-b", "lane/safe", "--",
                       str(tmp_path / "wt2"), full_sha], timeout=0.01)
    assert seen["killed"] is True


def test_s2_failed_add_cleanup_removes_branch_after_lock_release(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)

    def fail_verify(*_a, **_kw):
        raise lanes.LaneError("forced verifier failure")

    monkeypatch.setattr(cli, "_verify_lane_worktree", fail_verify)
    assert _run_raw(["lane", "assign", "--id", "failclean", "--from", "lead",
                     "--assignee", "dev", "--domain", "core", "--base", base,
                     "--target", _branch(root), "--worktrees-root",
                     str(root / ".worktrees")], root) == 2
    assert _git_rc(root, "rev-parse", "--verify",
                   "refs/heads/lane/failclean^{commit}").returncode != 0
    assert not list((root / ".worktrees").glob("failclean-*"))


def test_s3_gc_removes_cleanup_pending_worktree_and_keeps_unmerged_branch(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "later")
    wt = Path(lane["worktree_path"])
    _commit(wt, "src/core/a.py", "base\nlater\n")
    store = Store(root)
    store.write_launch_request({
        "request_id": "lr-later", "state": "queued", "lane_id": "later",
    })
    assert _run_raw(["lane", "deliver", "--id", "later", "--from", "dev"], root) == 0
    assert wt.exists()
    assert lanes.load_lanes(store)["lanes"]["later"]["worktree_state"] == lanes.STATUS_CLEANUP_PENDING
    assert store.archive_launch_request("lr-later", {"request_id": "lr-later",
                                                     "terminal_state": "archived"})
    capsys.readouterr()
    assert _run_raw(["lane", "gc", "--delete", "--json"], root) == 0
    payload = json.loads(capsys.readouterr().out)
    item = next(i for i in payload["items"] if i["lane_id"] == "later")
    assert item["worktree_removed"] is True
    assert item["branch_delete_safe"] is False
    assert _git_rc(root, "rev-parse", "--verify",
                   "refs/heads/lane/later^{commit}").returncode == 0
    assert not wt.exists()


def test_s4_detached_at_tip_passes_and_detached_other_holds(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "det")
    wt = Path(lane["worktree_path"])
    _commit(wt, "src/core/a.py", "base\ndetached\n")
    assert _git_rc(wt, "checkout", "--detach", "HEAD").returncode == 0
    assert _run_raw(["lane", "deliver", "--id", "det", "--from", "dev"], root) == 0
    art = next((Store(root).dir / "lane-deliveries").glob("det-*.json"))
    assert json.loads(art.read_text(encoding="utf-8"))["detached_at_lane_tip"] is True

    other = _assign_worktree(root, base, "detbad")
    other_wt = Path(other["worktree_path"])
    _commit(other_wt, "src/core/a.py", "base\ndetached bad\n")
    assert _git_rc(other_wt, "checkout", "--detach", base).returncode == 0
    assert _run_raw(["lane", "deliver", "--id", "detbad", "--from", "dev"], root) == 3


# --- C5a (0.40.1): lane delivery artifact readback before clearing the lane ----------

def test_validate_delivery_artifact_pure(tmp_path: Path) -> None:
    p = tmp_path / "art.json"
    good = {"schema_version": lanes.SCHEMA_VERSION, "lane_id": "l1", "delivered_head": "H1",
            "verdict": lanes.VERDICT_GO, "holds": []}

    def write(over=None):
        p.write_text(json.dumps({**good, **(over or {})}), encoding="utf-8")

    write()
    assert lanes.validate_delivery_artifact(p, lane_id="l1", head_sha="H1")["lane_id"] == "l1"
    # wrong lane/head
    write()
    with pytest.raises(lanes.LaneError):
        lanes.validate_delivery_artifact(p, lane_id="other", head_sha="H1")
    with pytest.raises(lanes.LaneError):
        lanes.validate_delivery_artifact(p, lane_id="l1", head_sha="OTHER")
    # SEMANTIC (reviewer-1 P1): valid JSON but wrong schema / non-GO verdict / dirty holds
    write({"schema_version": 999})
    with pytest.raises(lanes.LaneError):
        lanes.validate_delivery_artifact(p, lane_id="l1", head_sha="H1")
    write({"verdict": "HOLD"})
    with pytest.raises(lanes.LaneError):
        lanes.validate_delivery_artifact(p, lane_id="l1", head_sha="H1")
    write({"holds": [{"code": "x", "detail": "y"}]})       # GO with holds is inconsistent
    with pytest.raises(lanes.LaneError):
        lanes.validate_delivery_artifact(p, lane_id="l1", head_sha="H1")
    # missing field
    p.write_text(json.dumps({"lane_id": "l1", "delivered_head": "H1"}), encoding="utf-8")
    with pytest.raises(lanes.LaneError):
        lanes.validate_delivery_artifact(p, lane_id="l1", head_sha="H1")
    # unparseable
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(lanes.LaneError):
        lanes.validate_delivery_artifact(p, lane_id="l1", head_sha="H1")


def test_cli_deliver_corrupt_artifact_keeps_lane_active(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A corrupt PREPARED artifact can never become consumable GO. The state-first
    # checkpoint remains recoverably pending rather than reverting to active.
    root, base = _repo(tmp_path)
    br = _branch(root)
    _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
          "--domain", "core", "--base", base, "--target", br, "--path", "src/core"], root)
    head = _commit(root, "src/core/a.py", "base\nchange\n")

    real_prepare = lanes.write_prepared_delivery_artifact

    def bad_write(*args, **kwargs):  # noqa: ANN002,ANN003
        pending = real_prepare(*args, **kwargs)
        Path(pending["prepared_artifact"]).write_text("{corrupt artifact", encoding="utf-8")
        return pending

    monkeypatch.setattr(lanes, "write_prepared_delivery_artifact", bad_write)
    assert _run(["lane", "deliver", "--id", "l1", "--from", "dev", "--head", head], root) == 2
    saved = lanes.load_lanes(Store(root))["lanes"]["l1"]
    assert saved["status"] == lanes.STATUS_DELIVERED
    assert isinstance(saved["publish_pending"], dict)
    assert _committed_artifacts(Store(root), "l1") == []


def test_cli_deliver_valid_but_wrong_artifact_keeps_lane_active(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # C5a reviewer-1 P1: a VALID-JSON but semantically-wrong artifact (verdict HOLD) must
    # NOT clear the lane - the readback is semantic, not just structural.
    root, base = _repo(tmp_path)
    br = _branch(root)
    _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
          "--domain", "core", "--base", base, "--target", br, "--path", "src/core"], root)
    head = _commit(root, "src/core/a.py", "base\nchange\n")

    real_prepare = lanes.write_prepared_delivery_artifact

    def hold_write(*args, **kwargs):  # noqa: ANN002,ANN003
        pending = real_prepare(*args, **kwargs)
        path = Path(pending["prepared_artifact"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["verdict"] = "HOLD"
        payload["holds"] = [{"code": "x", "detail": "tampered"}]
        path.write_text(json.dumps(payload), encoding="utf-8")
        return pending

    monkeypatch.setattr(lanes, "write_prepared_delivery_artifact", hold_write)
    assert _run(["lane", "deliver", "--id", "l1", "--from", "dev", "--head", head], root) == 2
    saved = lanes.load_lanes(Store(root))["lanes"]["l1"]
    assert saved["status"] == lanes.STATUS_DELIVERED
    assert isinstance(saved["publish_pending"], dict)
    assert _committed_artifacts(Store(root), "l1") == []


# --- integrity-v3 recoverable delivery transaction ---------------------------------

def _advisory_delivery_fixture(root: Path, base: str, lane_id: str = "txn") -> str:
    assert _run([
        "lane", "assign", "--id", lane_id, "--from", "lead", "--assignee", "dev",
        "--domain", "core", "--base", base, "--target", _branch(root),
        "--path", "src/core",
    ], root) == 0
    return _commit(root, "src/core/a.py", f"base\n{lane_id}\n")


def _committed_artifacts(store: Store, lane_id: str) -> list[Path]:
    return sorted((store.dir / "lane-deliveries").glob(f"{lane_id}-*.json"))


def _prepared_artifacts(store: Store, lane_id: str) -> list[Path]:
    return sorted((store.dir / "lane-deliveries" / ".prepared").glob(
        f"{lane_id}-*.prepared.json"))


def test_delivery_first_state_save_failure_never_publishes_consumable_go(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, "savefail")
    store = Store(root)
    real_save = lanes.save_lanes
    failed = False

    def fail_first_delivered_save(target_store, data):  # noqa: ANN001
        nonlocal failed
        lane = data["lanes"]["savefail"]
        if lane.get("status") == lanes.STATUS_DELIVERED and not failed:
            failed = True
            raise OSError("injected first delivery state-save failure")
        return real_save(target_store, data)

    monkeypatch.setattr(lanes, "save_lanes", fail_first_delivered_save)
    assert _run_raw([
        "lane", "deliver", "--id", "savefail", "--from", "dev", "--head", head,
    ], root) == 2

    assert lanes.load_lanes(store)["lanes"]["savefail"]["status"] == lanes.STATUS_ACTIVE
    assert _committed_artifacts(store, "savefail") == []
    prepared = _prepared_artifacts(store, "savefail")
    assert len(prepared) <= 1
    if prepared:
        with pytest.raises(lanes.LaneError, match="prepared"):
            lanes.validate_delivery_artifact(
                prepared[0], lane_id="savefail", head_sha=head, store=store,
            )


def test_delivery_recovers_when_first_state_save_commits_then_reports_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, "save-ambiguous")
    store = Store(root)
    real_save = lanes.save_lanes
    failed = False

    def save_then_fail(target_store, data):  # noqa: ANN001
        nonlocal failed
        lane = data["lanes"]["save-ambiguous"]
        real_save(target_store, data)
        if lane.get("status") == lanes.STATUS_DELIVERED and not failed:
            failed = True
            raise OSError("injected failure after committed state save")

    monkeypatch.setattr(lanes, "save_lanes", save_then_fail)
    argv = [
        "lane", "deliver", "--id", "save-ambiguous", "--from", "dev", "--head", head,
    ]
    assert _run_raw(argv, root) == 2
    pending = lanes.load_lanes(store)["lanes"]["save-ambiguous"]
    assert pending["status"] == lanes.STATUS_DELIVERED
    assert isinstance(pending["publish_pending"], dict)
    assert len(_prepared_artifacts(store, "save-ambiguous")) == 1
    assert _committed_artifacts(store, "save-ambiguous") == []

    assert _run_raw(argv, root) == 0
    completed = lanes.load_lanes(store)["lanes"]["save-ambiguous"]
    assert completed["publish_pending"] is False
    assert len(_committed_artifacts(store, "save-ambiguous")) == 1


def test_committed_artifact_survives_reset_for_validation_and_close(
        tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "historical-reset")
    head = _commit(Path(lane["worktree_path"]), "src/core/a.py", "base\nhistorical\n")
    assert _run_raw([
        "lane", "deliver", "--id", "historical-reset", "--from", "dev",
    ], root) == 0
    store = Store(root)
    artifact = _committed_artifacts(store, "historical-reset")[0]

    assert _run_raw(["reset"], root) == 0
    assert lanes.load_lanes(store)["lanes"] == {}
    assert lanes.validate_delivery_artifact(
        artifact, lane_id="historical-reset", head_sha=head, store=store,
        require_isolation=True,
    )["artifact_state"] == lanes.ARTIFACT_COMMITTED
    assert _run_raw([
        "close", "open", "--id", "historical-reset-close", "--from", "lead",
        "--scope", "release", "--revision", head,
        "--lane-artifact", str(artifact),
    ], root) == 0
    assert _run_raw(["close", "check", "--id", "historical-reset-close"], root) == 0


def test_committed_artifact_survives_same_id_reassignment(
        tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "historical-reassign")
    head = _commit(Path(lane["worktree_path"]), "src/core/a.py", "base\nhistorical\n")
    assert _run_raw([
        "lane", "deliver", "--id", "historical-reassign", "--from", "dev",
    ], root) == 0
    store = Store(root)
    artifact = _committed_artifacts(store, "historical-reassign")[0]
    historical = json.loads(artifact.read_text(encoding="utf-8"))

    assert _run_raw([
        "lane", "assign", "--id", "historical-reassign", "--force", "--from", "lead",
        "--assignee", "dev", "--advisory", "--domain", "core", "--base", base,
        "--target", _branch(root), "--path", "src/core", "--no-worktree",
        "--worktree-waiver-reason", "new advisory generation",
    ], root) == 0
    replacement = lanes.load_lanes(store)["lanes"]["historical-reassign"]
    assert replacement["instance_id"] != historical["lane_instance_id"]
    assert lanes.validate_delivery_artifact(
        artifact, lane_id="historical-reassign", head_sha=head, store=store,
        require_isolation=True,
    )["transaction_id"] == historical["transaction_id"]
    assert _git_rc(
        root, "update-ref", "-d", lanes.lane_ref("historical-reassign"),
    ).returncode == 0
    assert not _git(root, "show-ref", "--verify", lanes.lane_ref("historical-reassign"))
    assert _run_raw([
        "close", "open", "--id", "historical-reassign-close", "--from", "lead",
        "--scope", "release", "--revision", head,
        "--lane-artifact", str(artifact),
    ], root) == 0
    assert _run_raw(["close", "check", "--id", "historical-reassign-close"], root) == 0


def _leave_publish_pending(
    root: Path, base: str, lane_id: str, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Store, dict]:
    head = _advisory_delivery_fixture(root, base, lane_id)
    monkeypatch.setattr(
        lanes, "publish_delivery_artifact",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("stop before publication")),
    )
    assert _run_raw([
        "lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head,
    ], root) == 2
    store = Store(root)
    pending = lanes.load_lanes(store)["lanes"][lane_id]
    assert isinstance(pending["publish_pending"], dict)
    return store, pending


def _leave_rebound_publish_pending(
        root: Path, argv: list[str], lane_id: str,
        monkeypatch: pytest.MonkeyPatch) -> tuple[Store, dict]:
    real_publish = lanes.publish_delivery_artifact
    crashed = False

    def crash_once(*args, **kwargs):  # noqa: ANN002,ANN003
        nonlocal crashed
        if not crashed:
            crashed = True
            raise OSError("injected crash after terminal rebind")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(lanes, "publish_delivery_artifact", crash_once)
    assert _run_raw(argv, root) == 2
    monkeypatch.setattr(lanes, "publish_delivery_artifact", real_publish)
    store = Store(root)
    pending = lanes.load_lanes(store)["lanes"][lane_id]
    assert isinstance(pending["publish_pending"], dict)
    assert pending["publish_pending"]["terminal_rebound"] is True
    assert _committed_artifacts(store, lane_id) == []
    return store, pending


@pytest.mark.parametrize(
    ("lane_kind", "artifact_case"),
    [
        ("advisory", "unsigned"), ("release", "unsigned"),
        ("advisory", "legacy"), ("release", "legacy"),
        ("advisory", "instance"), ("advisory", "generation"),
        ("advisory", "evaluation"),
    ],
)
def test_existing_final_recovery_requires_exact_current_committed_evidence(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture, lane_kind: str, artifact_case: str) -> None:
    root, base = _repo(tmp_path)
    lane_id = f"strict-{lane_kind}-{artifact_case}"
    if lane_kind == "release":
        lane = _assign_worktree(root, base, lane_id)
        head = _commit(
            Path(lane["worktree_path"]), "src/core/a.py", "base\nstrict final\n",
        )
        argv = ["lane", "deliver", "--id", lane_id, "--from", "dev"]
    else:
        head = _advisory_delivery_fixture(root, base, lane_id)
        argv = [
            "lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head,
        ]
    store, lane_state = _leave_rebound_publish_pending(
        root, argv, lane_id, monkeypatch,
    )
    pending = lane_state["publish_pending"]
    prepared = Path(pending["prepared_artifact"])
    final = Path(pending["committed_artifact"])
    payload = json.loads(prepared.read_text(encoding="utf-8"))
    payload["artifact_state"] = lanes.ARTIFACT_COMMITTED
    if artifact_case == "unsigned":
        payload.pop("integrity_token", None)
    elif artifact_case == "legacy":
        payload["integrity_version"] = lanes.LEGACY_INTEGRITY_VERSION
        payload["evaluation_snapshot"]["evaluation_version"] = \
            lanes.LEGACY_EVALUATION_VERSION
        payload["integrity_token"] = lanes.compute_integrity_token(store, payload)
    elif artifact_case == "instance":
        payload["lane_instance_id"] = (
            "f" * 32 if payload["lane_instance_id"] != "f" * 32 else "e" * 32
        )
        payload["integrity_token"] = lanes.compute_integrity_token(store, payload)
    elif artifact_case == "generation":
        payload["lane_generation"] += 1
        payload["integrity_token"] = lanes.compute_integrity_token(store, payload)
    else:
        payload["evaluation_snapshot"]["gate_digest"] = "0" * 64
        payload["integrity_token"] = lanes.compute_integrity_token(store, payload)
    final.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    original_bytes = final.read_bytes()

    assert _run_raw(argv, root) == 2
    assert final.read_bytes() == original_bytes
    saved = lanes.load_lanes(store)["lanes"][lane_id]
    assert isinstance(saved["publish_pending"], dict)
    assert saved["delivery_transaction_id"] == pending["transaction_id"]
    if lane_kind == "release" and artifact_case == "legacy":
        capsys.readouterr()
        assert _run_raw(["lane", "status"], root) == 0
        assert "final_invalid" in capsys.readouterr().out
        assert _run_raw(["reset"], root) == 2
        assert _run_raw([
            "lane", "recover", "--id", lane_id,
            "--reason", "quarantine rejected legacy final and reevaluate",
        ], root) == 0
        recovered = lanes.load_lanes(store)["lanes"][lane_id]
        quarantined = Path(recovered["delivery_recovery"]["quarantined_final"])
        assert not final.exists()
        assert quarantined.exists()
        with pytest.raises(lanes.LaneError, match="committed directory"):
            lanes.validate_delivery_artifact(
                quarantined, lane_id=lane_id, head_sha=head, store=store,
                require_isolation=True,
            )
        assert _run_raw(argv, root) == 0


def test_force_assign_refuses_delivered_publish_pending_lane(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    store, pending = _leave_publish_pending(root, base, "pending-force", monkeypatch)

    assert _run_raw([
        "lane", "assign", "--id", "pending-force", "--force", "--from", "lead",
        "--assignee", "dev", "--advisory", "--domain", "core", "--base", base,
        "--target", _branch(root), "--path", "src/core", "--no-worktree",
        "--worktree-waiver-reason", "must not overwrite pending",
    ], root) == 2
    saved = lanes.load_lanes(store)["lanes"]["pending-force"]
    assert saved["delivery_transaction_id"] == pending["delivery_transaction_id"]
    assert isinstance(saved["publish_pending"], dict)


def test_abandon_refuses_delivered_publish_pending_lane(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    store, pending = _leave_publish_pending(root, base, "pending-abandon", monkeypatch)

    assert _run_raw(["lane", "abandon", "--id", "pending-abandon"], root) == 2
    saved = lanes.load_lanes(store)["lanes"]["pending-abandon"]
    assert saved["delivery_transaction_id"] == pending["delivery_transaction_id"]
    assert saved["status"] == lanes.STATUS_DELIVERED
    assert isinstance(saved["publish_pending"], dict)


@pytest.mark.parametrize(
    ("damage", "diagnosis"),
    [("missing", "prepared_missing"), ("tampered", "prepared_invalid")],
)
def test_operator_can_recover_pending_delivery_with_broken_prepared_artifact(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture, damage: str, diagnosis: str) -> None:
    root, base = _repo(tmp_path)
    lane_id = f"recover-prepared-{damage}"
    head = _advisory_delivery_fixture(root, base, lane_id)
    argv = [
        "lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head,
    ]
    store, lane_state = _leave_rebound_publish_pending(
        root, argv, lane_id, monkeypatch,
    )
    prepared = Path(lane_state["publish_pending"]["prepared_artifact"])
    if damage == "missing":
        prepared.unlink()
    else:
        prepared.write_text('{"tampered": true}', encoding="utf-8")

    assert _run_raw(argv, root) == 2
    capsys.readouterr()
    assert _run_raw(["lane", "status"], root) == 0
    assert diagnosis in capsys.readouterr().out
    assert _run_raw(["reset"], root) == 2
    assert lanes.load_lanes(store)["lanes"][lane_id]["status"] == lanes.STATUS_DELIVERED

    assert _run_raw([
        "lane", "recover", "--id", lane_id,
        "--reason", "discard broken prepared evidence and reevaluate",
    ], root) == 0
    recovered = lanes.load_lanes(store)["lanes"][lane_id]
    assert recovered["status"] == lanes.STATUS_ACTIVE
    assert _run_raw(argv, root) == 0
    assert len(_committed_artifacts(store, lane_id)) == 1


@pytest.mark.parametrize(
    ("marker_case", "diagnosis"),
    [("missing", "marker_missing"), ("scalar", "marker_corrupt")],
)
def test_operator_can_recover_delivered_lane_with_corrupt_publication_marker(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture, marker_case: str, diagnosis: str) -> None:
    root, base = _repo(tmp_path)
    lane_id = f"recover-marker-{marker_case}"
    head = _advisory_delivery_fixture(root, base, lane_id)
    argv = [
        "lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head,
    ]
    store, _lane_state = _leave_rebound_publish_pending(
        root, argv, lane_id, monkeypatch,
    )
    data = lanes.load_lanes(store)
    if marker_case == "missing":
        data["lanes"][lane_id].pop("publish_pending", None)
    else:
        data["lanes"][lane_id]["publish_pending"] = "corrupt"
    lanes.save_lanes(store, data)

    capsys.readouterr()
    assert _run_raw(["lane", "status"], root) == 0
    assert diagnosis in capsys.readouterr().out
    assert _run_raw(["reset"], root) == 2
    assert lane_id in lanes.load_lanes(store)["lanes"]
    assert _run_raw([
        "lane", "recover", "--id", lane_id,
        "--reason", "restore corrupt publication marker to active evaluation",
    ], root) == 0
    assert lanes.load_lanes(store)["lanes"][lane_id]["status"] == lanes.STATUS_ACTIVE
    assert _run_raw(argv, root) == 0
    assert len(_committed_artifacts(store, lane_id)) == 1


@pytest.mark.parametrize("marker_case", ["missing", "scalar"])
def test_corrupt_marker_recovery_quarantines_an_existing_final(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        marker_case: str) -> None:
    root, base = _repo(tmp_path)
    lane_id = f"recover-final-{marker_case}"
    head = _advisory_delivery_fixture(root, base, lane_id)
    argv = [
        "lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head,
    ]
    real_checkpoint = cli._lane_checkpoint_publication
    monkeypatch.setattr(
        cli, "_lane_checkpoint_publication",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("marker save crash")),
    )
    assert _run_raw(argv, root) == 2
    monkeypatch.setattr(cli, "_lane_checkpoint_publication", real_checkpoint)

    store = Store(root)
    data = lanes.load_lanes(store)
    pending = data["lanes"][lane_id]["publish_pending"]
    final = Path(pending["committed_artifact"])
    assert final.exists()
    if marker_case == "missing":
        data["lanes"][lane_id].pop("publish_pending")
    else:
        data["lanes"][lane_id]["publish_pending"] = "corrupt"
    lanes.save_lanes(store, data)

    assert _run_raw([
        "lane", "recover", "--id", lane_id,
        "--reason", "quarantine final whose publication marker was lost",
    ], root) == 0
    recovered = lanes.load_lanes(store)["lanes"][lane_id]
    quarantine = Path(recovered["delivery_recovery"]["quarantined_final"])
    assert recovered["status"] == lanes.STATUS_ACTIVE
    assert not final.exists()
    assert quarantine.exists()
    with pytest.raises(lanes.LaneError, match="committed directory"):
        lanes.validate_delivery_artifact(
            quarantine, lane_id=lane_id, head_sha=head, store=store,
        )


def test_recover_serializes_against_pending_delivery_publication(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    lane_id = "recover-publication-race"
    head = _advisory_delivery_fixture(root, base, lane_id)
    argv = [
        "lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head,
    ]
    store, _pending = _leave_rebound_publish_pending(
        root, argv, lane_id, monkeypatch,
    )

    real_recover = cli._lane_recover_delivery
    real_prepare = cli._lane_prepare_publication
    recovery_entered = threading.Event()
    release_recovery = threading.Event()
    publication_entered = threading.Event()

    def paused_recovery(*args, **kwargs):  # noqa: ANN002,ANN003
        recovery_entered.set()
        assert release_recovery.wait(5)
        return real_recover(*args, **kwargs)

    def observed_publication(*args, **kwargs):  # noqa: ANN002,ANN003
        publication_entered.set()
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr(cli, "_lane_recover_delivery", paused_recovery)
    monkeypatch.setattr(cli, "_lane_prepare_publication", observed_publication)
    with ThreadPoolExecutor(max_workers=2) as pool:
        recover_result = pool.submit(
            _run_raw,
            ["lane", "recover", "--id", lane_id, "--reason", "discard attempt"],
            root,
        )
        assert recovery_entered.wait(5)
        delivery_result = pool.submit(_run_raw, argv, root)
        raced = publication_entered.wait(1)
        release_recovery.set()
        assert recover_result.result(timeout=10) == 0
        assert delivery_result.result(timeout=10) == 2

    assert raced is False
    assert lanes.load_lanes(store)["lanes"][lane_id]["status"] == lanes.STATUS_ACTIVE


def test_reset_serializes_with_first_pending_delivery_save(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    lane_id = "reset-delivery-race"
    head = _advisory_delivery_fixture(root, base, lane_id)
    argv = [
        "lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head,
    ]
    reset_entered = threading.Event()
    release_reset = threading.Event()
    pending_saved = threading.Event()
    real_reset = Store.reset
    real_save = lanes.save_lanes

    def paused_reset(self, *args, **kwargs):  # noqa: ANN001,ANN002,ANN003
        reset_entered.set()
        assert release_reset.wait(5)
        return real_reset(self, *args, **kwargs)

    def observed_save(store, data):  # noqa: ANN001
        result = real_save(store, data)
        lane = (data.get("lanes") or {}).get(lane_id)
        if (isinstance(lane, dict)
                and lane.get("status") == lanes.STATUS_DELIVERED
                and isinstance(lane.get("publish_pending"), dict)):
            pending_saved.set()
        return result

    monkeypatch.setattr(Store, "reset", paused_reset)
    monkeypatch.setattr(lanes, "save_lanes", observed_save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reset_result = pool.submit(_run_raw, ["reset"], root)
        assert reset_entered.wait(5)
        delivery_result = pool.submit(_run_raw, argv, root)
        raced = pending_saved.wait(1)
        release_reset.set()
        assert reset_result.result(timeout=10) == 0
        assert delivery_result.result(timeout=10) in {2, 3}

    assert raced is False
    assert lanes.load_lanes(Store(root))["lanes"] == {}


def test_delivered_retry_refuses_explicit_mismatched_head(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, "retry-head")
    assert _run_raw([
        "lane", "deliver", "--id", "retry-head", "--from", "dev", "--head", head,
    ], root) == 0

    assert _run_raw([
        "lane", "deliver", "--id", "retry-head", "--from", "dev", "--head", base,
    ], root) == 3


def test_cooperating_message_token_detects_rapid_message_with_frozen_mtime(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _base = _repo(tmp_path)
    store = Store(root)
    messages = store.messages_dir
    fixed_stat = messages.stat()
    real_stat = Path.stat

    def frozen_message_stat(path, *args, **kwargs):  # noqa: ANN001
        if path == messages:
            return fixed_stat
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", frozen_message_stat)
    before = lanes.cooperating_input_fingerprint(store)
    store.send(
        sender="lead", recipient="dev", body="rapid barrier",
        meta={"barrier": {"version": 1, "scope": "global", "type": "release"}},
    )
    after = lanes.cooperating_input_fingerprint(store)

    assert before != after


def test_unrelated_message_during_evaluation_does_not_invalidate_delivery(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    lane_id = "unrelated-message"
    head = _advisory_delivery_fixture(root, base, lane_id)
    store = Store(root)
    real_prepare = lanes.write_prepared_delivery_artifact

    def send_unrelated_after_prepare(*args, **kwargs):  # noqa: ANN002,ANN003
        pending = real_prepare(*args, **kwargs)
        store.send(
            sender="lead", recipient="dev", kind="note",
            body="ordinary coordination traffic unrelated to lane verdict",
        )
        return pending

    monkeypatch.setattr(
        lanes, "write_prepared_delivery_artifact", send_unrelated_after_prepare,
    )
    assert _run_raw([
        "lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head,
    ], root) == 0
    assert len(_committed_artifacts(store, lane_id)) == 1


def test_barrier_token_detects_atomic_replacement_and_deletion_fallback(
        tmp_path: Path) -> None:
    root, _base = _repo(tmp_path)
    store = Store(root)
    earlier = store.send(
        sender="lead", recipient="lead", body="earlier barrier",
        meta={"barrier": {"version": 1, "scope": "global", "type": "release"}},
    )
    latest = store.send(
        sender="lead", recipient="lead", body="latest barrier",
        meta={"barrier": {"version": 1, "scope": "global", "type": "release"}},
    )
    latest_path = store.messages_dir / f"{latest.id}.json"
    original = lanes.cooperating_input_fingerprint(store)

    payload = json.loads(latest_path.read_text(encoding="utf-8"))
    payload["body"] = "atomically replaced latest barrier"
    replacement = store.dir / "barrier-replacement.json"
    replacement.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(replacement, latest_path)
    replaced = lanes.cooperating_input_fingerprint(store)
    assert store.current_epoch() == latest.id
    assert replaced != original

    latest_path.unlink()
    fallback = lanes.cooperating_input_fingerprint(store)
    assert store.current_epoch() == earlier.id
    assert fallback != replaced


def test_delivery_terminal_rebind_rejects_target_move_after_last_git_recheck(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    target_ref = _branch(root)
    lane = _assign_worktree(root, base, "terminal-target")
    _commit(
        Path(lane["worktree_path"]), "src/core/a.py", "base\nlane candidate\n",
    )
    _git(root, "checkout", "-q", "-b", "terminal-target-conflict", base)
    conflicting = _commit(root, "src/core/a.py", "base\ntarget conflict\n")
    _git(root, "checkout", "-q", target_ref)
    assert _git(root, "rev-parse", "HEAD").strip() == base
    real_prepare = lanes.write_prepared_delivery_artifact

    def move_target_after_prepare(*args, **kwargs):  # noqa: ANN002,ANN003
        pending = real_prepare(*args, **kwargs)
        _git(root, "update-ref", f"refs/heads/{target_ref}", conflicting, base)
        assert _git(root, "rev-parse", target_ref).strip() == conflicting
        return pending

    monkeypatch.setattr(lanes, "write_prepared_delivery_artifact", move_target_after_prepare)
    assert _run_raw([
        "lane", "deliver", "--id", "terminal-target", "--from", "dev",
    ], root) == 3
    saved = lanes.load_lanes(Store(root))["lanes"]["terminal-target"]
    assert saved["status"] == lanes.STATUS_ACTIVE
    assert _committed_artifacts(Store(root), "terminal-target") == []


def test_delivery_terminal_rebind_rejects_gate_red_after_final_fingerprint_read(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    gates.set_gate(
        root, name="ci", status="green", severity="blocker", scope="release",
        actor="lead", evidence_source="automation_ci", evidence=["ci://green"],
        required=True,
    )
    head = _advisory_delivery_fixture(root, base, "terminal-gate")
    real_fingerprint = lanes.cooperating_input_fingerprint
    reads = 0

    def turn_gate_red_after_read(store):  # noqa: ANN001
        nonlocal reads
        token = real_fingerprint(store)
        reads += 1
        if reads == 3:
            gates.set_gate(
                root, name="ci", status="red", severity="blocker", scope="release",
                actor="lead", evidence_source="local_command", reason="late failure",
            )
        return token

    monkeypatch.setattr(lanes, "cooperating_input_fingerprint", turn_gate_red_after_read)
    assert _run_raw([
        "lane", "deliver", "--id", "terminal-gate", "--from", "dev",
        "--head", head, "--gate-scope", "release",
    ], root) == 3
    saved = lanes.load_lanes(Store(root))["lanes"]["terminal-gate"]
    assert saved["status"] == lanes.STATUS_ACTIVE
    assert _committed_artifacts(Store(root), "terminal-gate") == []


def test_delivery_terminal_rebind_rejects_dirty_worktree_after_provenance_recheck(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "terminal-dirty")
    worktree = Path(lane["worktree_path"])
    _commit(worktree, "src/core/a.py", "base\ncommitted candidate\n")
    real_prepare = lanes.write_prepared_delivery_artifact

    def dirty_after_prepare(*args, **kwargs):  # noqa: ANN002,ANN003
        pending = real_prepare(*args, **kwargs)
        (worktree / "src/core/a.py").write_text(
            "base\ncommitted candidate\nuncommitted change\n", encoding="utf-8",
        )
        return pending

    monkeypatch.setattr(lanes, "write_prepared_delivery_artifact", dirty_after_prepare)
    assert _run_raw([
        "lane", "deliver", "--id", "terminal-dirty", "--from", "dev",
    ], root) == 3
    saved = lanes.load_lanes(Store(root))["lanes"]["terminal-dirty"]
    assert saved["status"] == lanes.STATUS_ACTIVE
    assert _committed_artifacts(Store(root), "terminal-dirty") == []


def test_delivery_terminal_rebind_hashes_in_place_barrier_contents(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    store = Store(root)
    barrier = store.send(
        sender="lead", recipient="lead", body="release barrier",
        meta={"barrier": {"version": 1, "scope": "global", "type": "release"}},
    )
    message_path = store.messages_dir / f"{barrier.id}.json"
    head = _advisory_delivery_fixture(root, base, "terminal-barrier")
    message_stat = message_path.stat()
    directory_stat = store.messages_dir.stat()
    names_before = sorted(path.name for path in store.messages_dir.iterdir())
    real_prepare = lanes.write_prepared_delivery_artifact

    def rewrite_barrier_after_prepare(*args, **kwargs):  # noqa: ANN002,ANN003
        pending = real_prepare(*args, **kwargs)
        raw = message_path.read_bytes()
        before = b'"type": "release"'
        after = b'"type": "changed"'
        assert len(before) == len(after)
        assert raw.count(before) == 1
        message_path.write_bytes(raw.replace(before, after, 1))
        os.utime(message_path, ns=(message_stat.st_atime_ns, message_stat.st_mtime_ns))
        os.utime(
            store.messages_dir,
            ns=(directory_stat.st_atime_ns, directory_stat.st_mtime_ns),
        )
        assert sorted(path.name for path in store.messages_dir.iterdir()) == names_before
        assert store.messages_dir.stat().st_mtime_ns == directory_stat.st_mtime_ns
        assert message_path.stat().st_size == message_stat.st_size
        assert store.current_epoch() == barrier.id
        return pending

    monkeypatch.setattr(lanes, "write_prepared_delivery_artifact", rewrite_barrier_after_prepare)
    assert _run_raw([
        "lane", "deliver", "--id", "terminal-barrier", "--from", "dev",
        "--head", head,
    ], root) == 3
    saved = lanes.load_lanes(store)["lanes"]["terminal-barrier"]
    assert saved["status"] == lanes.STATUS_ACTIVE
    assert _committed_artifacts(store, "terminal-barrier") == []


def test_provisional_delivery_retains_overlapping_path_reservation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, "reserved-a")
    store = Store(root)
    real_prepare = cli._lane_prepare_publication
    attempted_overlap = False

    def reject_overlap_before_rebind(target_store, lane_id):  # noqa: ANN001
        nonlocal attempted_overlap
        if not attempted_overlap:
            attempted_overlap = True
            assert lane_id == "reserved-a"
            assert _run([
                "lane", "assign", "--id", "reserved-b", "--from", "lead",
                "--assignee", "dev", "--domain", "core", "--base", base,
                "--target", _branch(root), "--path", "src/core",
            ], root) == 2
        return real_prepare(target_store, lane_id)

    monkeypatch.setattr(cli, "_lane_prepare_publication", reject_overlap_before_rebind)
    assert _run_raw([
        "lane", "deliver", "--id", "reserved-a", "--from", "dev",
        "--head", head,
    ], root) == 0
    data = lanes.load_lanes(store)
    assert "reserved-b" not in data["lanes"]
    assert data["lanes"]["reserved-a"]["publish_pending"] is False
    assert len(_committed_artifacts(store, "reserved-a")) == 1


def test_terminal_rollback_holds_for_legacy_overlap_injected_after_provisional_save(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, "rollback-a")
    store = Store(root)
    real_prepare = cli._lane_prepare_publication
    injected_overlap = False

    def inject_overlap_before_rebind(target_store, lane_id):  # noqa: ANN001
        nonlocal injected_overlap
        if not injected_overlap:
            injected_overlap = True
            assert lane_id == "rollback-a"
            with target_store._config_lock():
                data = lanes.load_lanes(target_store)
                source = data["lanes"]["rollback-a"]["publish_pending"]["lane_snapshot"]
                data["lanes"]["rollback-b"] = lanes.new_lane(
                    "rollback-b", assignee="dev", assigned_by="legacy",
                    assigned_at="injected", domain_id="core",
                    path_subset=["src/core"], base_sha=base,
                    target_ref=_branch(root), target_head_at_assign=head,
                    epoch_at_assign=target_store.current_epoch(),
                    registry_hash_at_assign=source["registry_hash_at_assign"],
                    release_class=False,
                    waiver={"reason": "legacy overlap", "by": "legacy",
                            "at": "injected", "authority": "advisory_only"},
                )
                lanes.save_lanes(target_store, data)
        return real_prepare(target_store, lane_id)

    monkeypatch.setattr(cli, "_lane_prepare_publication", inject_overlap_before_rebind)
    argv = [
        "lane", "deliver", "--id", "rollback-a", "--from", "dev",
        "--head", head,
    ]

    assert _run_raw(argv, root) == 3
    first_error = capsys.readouterr().err
    assert "nonconsumable terminal HOLD" in first_error
    assert "rollback-b" in first_error
    assert "retry lane deliver" in first_error

    data = lanes.load_lanes(store)
    held = data["lanes"]["rollback-a"]
    assert held["status"] == lanes.STATUS_DELIVERED
    assert isinstance(held["publish_pending"], dict)
    assert held["publish_pending"]["terminal_rebound"] is False
    assert held["terminal_hold"]["conflicting_lane_ids"] == ["rollback-b"]
    assert [lane["lane_id"] for lane in lanes.active_lanes(data)] == ["rollback-b"]
    assert len(_prepared_artifacts(store, "rollback-a")) == 1
    assert _committed_artifacts(store, "rollback-a") == []
    capsys.readouterr()
    assert _run_raw(["lane", "status"], root) == 0
    status_output = capsys.readouterr().out
    assert "rollback-a" in status_output
    assert "terminal_hold" in status_output
    assert _run_raw(["reset"], root) == 2
    assert "rollback-a" in lanes.load_lanes(store)["lanes"]

    assert _run_raw(argv, root) == 3
    retry_error = capsys.readouterr().err
    assert "nonconsumable terminal HOLD" in retry_error
    assert "rollback-b" in retry_error
    retry_data = lanes.load_lanes(store)
    assert [lane["lane_id"] for lane in lanes.active_lanes(retry_data)] == ["rollback-b"]
    assert _committed_artifacts(store, "rollback-a") == []

    assert _run_raw(["lane", "abandon", "--id", "rollback-b"], root) == 0
    assert _run_raw(argv, root) == 0
    final = lanes.load_lanes(store)["lanes"]["rollback-a"]
    assert final["publish_pending"] is False
    assert "terminal_hold" not in final
    assert len(_committed_artifacts(store, "rollback-a")) == 1


@pytest.mark.parametrize("drift", ["gate", "target", "message", "worktree"])
def test_recovery_without_final_freshly_rebinds_live_inputs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str) -> None:
    root, base = _repo(tmp_path)
    store = Store(root)
    target_ref = _branch(root)
    conflicting = None
    message_path = None

    if drift == "gate":
        gates.set_gate(
            root, name="ci", status="green", severity="blocker", scope="release",
            actor="lead", evidence_source="automation_ci", evidence=["ci://green"],
            required=True,
        )
    elif drift == "target":
        _git(root, "checkout", "-q", "-b", "recovery-target-conflict", base)
        conflicting = _commit(root, "src/core/a.py", "base\nconflicting target\n")
        _git(root, "checkout", "-q", target_ref)
    elif drift == "message":
        barrier = store.send(
            sender="lead", recipient="lead", body="release barrier",
            meta={"barrier": {"version": 1, "scope": "global", "type": "release"}},
        )
        message_path = store.messages_dir / f"{barrier.id}.json"

    lane_id = f"recovery-drift-{drift}"
    if drift == "worktree":
        lane = _assign_worktree(root, base, lane_id)
        worktree = Path(lane["worktree_path"])
        head = _commit(worktree, "src/core/a.py", "base\nrecovery candidate\n")
        argv = ["lane", "deliver", "--id", lane_id, "--from", "dev"]
    else:
        head = _advisory_delivery_fixture(root, base, lane_id)
        argv = [
            "lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head,
        ]
        if drift == "gate":
            argv.extend(["--gate-scope", "release"])

    real_publish = lanes.publish_delivery_artifact
    crashed = False

    def crash_after_rebind(*args, **kwargs):  # noqa: ANN002,ANN003
        nonlocal crashed
        if not crashed:
            crashed = True
            raise OSError("injected crash after terminal rebind")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(lanes, "publish_delivery_artifact", crash_after_rebind)
    assert _run_raw(argv, root) == 2
    pending = lanes.load_lanes(store)["lanes"][lane_id]
    assert pending["publish_pending"]["terminal_rebound"] is True
    assert _committed_artifacts(store, lane_id) == []

    if drift == "gate":
        gates.set_gate(
            root, name="ci", status="red", severity="blocker", scope="release",
            actor="lead", evidence_source="local_command", reason="late failure",
        )
    elif drift == "target":
        assert conflicting is not None
        _git(root, "update-ref", f"refs/heads/{target_ref}", conflicting, head)
    elif drift == "message":
        assert message_path is not None
        message_stat = message_path.stat()
        directory_stat = store.messages_dir.stat()
        payload = json.loads(message_path.read_text(encoding="utf-8"))
        payload["meta"]["barrier"]["type"] = "changed"
        message_path.write_bytes(
            json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
        )
        os.utime(message_path, ns=(message_stat.st_atime_ns, message_stat.st_mtime_ns))
        os.utime(
            store.messages_dir,
            ns=(directory_stat.st_atime_ns, directory_stat.st_mtime_ns),
        )
    else:
        (worktree / "src/core/a.py").write_text(
            "base\nrecovery candidate\nlate dirty change\n", encoding="utf-8",
        )

    assert _run_raw(argv, root) == 3
    saved = lanes.load_lanes(store)["lanes"][lane_id]
    assert saved["status"] == lanes.STATUS_ACTIVE
    assert _committed_artifacts(store, lane_id) == []


@pytest.mark.parametrize("action", ["force", "abandon"])
@pytest.mark.parametrize(
    ("marker_case", "marker", "expected_rc"),
    [("true", True, 2), ("scalar", "corrupt", 2), ("missing", None, 2),
     ("complete", False, 0)],
)
def test_delivered_lane_mutation_requires_explicit_complete_publication_marker(
        tmp_path: Path, action: str, marker_case: str, marker: object,
        expected_rc: int) -> None:
    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, f"marker-{action}-{marker_case}")
    lane_id = f"marker-{action}-{marker_case}"
    store = Store(root)
    data = lanes.load_lanes(store)
    previous = data["lanes"][lane_id]
    previous_instance = previous["instance_id"]
    previous["status"] = lanes.STATUS_DELIVERED
    previous["delivered_head"] = head
    previous["delivery_transaction_id"] = "f" * 32
    if marker_case == "missing":
        previous.pop("publish_pending", None)
    else:
        previous["publish_pending"] = marker
    lanes.save_lanes(store, data)

    if action == "force":
        rc = _run_raw([
            "lane", "assign", "--id", lane_id, "--force", "--from", "lead",
            "--assignee", "dev", "--advisory", "--domain", "core", "--base", base,
            "--target", _branch(root), "--path", "src/core", "--no-worktree",
            "--worktree-waiver-reason", "marker policy fixture",
        ], root)
    else:
        rc = _run_raw(["lane", "abandon", "--id", lane_id], root)

    assert rc == expected_rc
    saved = lanes.load_lanes(store)["lanes"][lane_id]
    if expected_rc == 2:
        assert saved["instance_id"] == previous_instance
        assert saved["status"] == lanes.STATUS_DELIVERED
    elif action == "force":
        assert saved["instance_id"] != previous_instance
        assert saved["status"] == lanes.STATUS_ACTIVE
    else:
        assert saved["status"] == lanes.STATUS_ABANDONED


@pytest.mark.parametrize("marker_case", ["true", "scalar", "missing"])
def test_corrupt_delivered_marker_retains_path_reservation(
        tmp_path: Path, marker_case: str) -> None:
    root, base = _repo(tmp_path)
    _advisory_delivery_fixture(root, base, f"reserved-corrupt-{marker_case}")
    store = Store(root)
    data = lanes.load_lanes(store)
    lane = data["lanes"][f"reserved-corrupt-{marker_case}"]
    lane["status"] = lanes.STATUS_DELIVERED
    if marker_case == "true":
        lane["publish_pending"] = True
    elif marker_case == "scalar":
        lane["publish_pending"] = "corrupt"
    else:
        lane.pop("publish_pending", None)
    lanes.save_lanes(store, data)

    assert _run([
        "lane", "assign", "--id", f"blocked-by-{marker_case}", "--from", "lead",
        "--assignee", "dev", "--domain", "core", "--base", base,
        "--target", _branch(root), "--path", "src/core",
    ], root) == 2
    assert f"blocked-by-{marker_case}" not in lanes.load_lanes(store)["lanes"]


def test_delivery_retry_after_publish_failure_freshly_rebinds(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, "prepub")
    store = Store(root)
    real_eval = cli._lane_eval
    evals = 0

    def count_eval(*args, **kwargs):  # noqa: ANN002,ANN003
        nonlocal evals
        evals += 1
        return real_eval(*args, **kwargs)

    real_publish = lanes.publish_delivery_artifact
    failed = False

    def fail_once(*args, **kwargs):  # noqa: ANN002,ANN003
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected crash before publication")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(cli, "_lane_eval", count_eval)
    monkeypatch.setattr(lanes, "publish_delivery_artifact", fail_once)
    argv = ["lane", "deliver", "--id", "prepub", "--from", "dev", "--head", head]
    assert _run_raw(argv, root) == 2
    pending = lanes.load_lanes(store)["lanes"]["prepub"]
    assert pending["status"] == lanes.STATUS_DELIVERED
    assert isinstance(pending.get("publish_pending"), dict)
    assert _committed_artifacts(store, "prepub") == []

    assert _run_raw(argv, root) == 0
    assert evals == 3  # initial GO, initial terminal rebind, and recovery rebind
    final = lanes.load_lanes(store)["lanes"]["prepub"]
    assert final["publish_pending"] is False
    assert len(_committed_artifacts(store, "prepub")) == 1


def test_delivery_retry_after_atomic_final_write_failure_freshly_rebinds(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agenttalk import _atomic

    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, "finalwrite")
    store = Store(root)
    real_eval = cli._lane_eval
    evals = 0

    def count_eval(*args, **kwargs):  # noqa: ANN002,ANN003
        nonlocal evals
        evals += 1
        return real_eval(*args, **kwargs)

    real_write = _atomic.write_text
    failed = False

    def fail_final(path, text):  # noqa: ANN001
        nonlocal failed
        candidate = Path(path)
        if (not failed and candidate.parent == store.dir / "lane-deliveries"
                and candidate.name.startswith("finalwrite-")):
            failed = True
            raise OSError("injected atomic final-write failure")
        return real_write(path, text)

    monkeypatch.setattr(cli, "_lane_eval", count_eval)
    monkeypatch.setattr(_atomic, "write_text", fail_final)
    argv = ["lane", "deliver", "--id", "finalwrite", "--from", "dev", "--head", head]
    assert _run_raw(argv, root) == 2
    assert isinstance(lanes.load_lanes(store)["lanes"]["finalwrite"]["publish_pending"], dict)
    assert _committed_artifacts(store, "finalwrite") == []

    assert _run_raw(argv, root) == 0
    assert evals == 3  # initial GO, initial terminal rebind, and recovery rebind
    assert len(_committed_artifacts(store, "finalwrite")) == 1


def test_delivery_retry_after_final_write_and_marker_save_failure_is_idempotent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, "postpub")
    store = Store(root)
    real_eval = cli._lane_eval
    evals = 0

    def count_eval(*args, **kwargs):  # noqa: ANN002,ANN003
        nonlocal evals
        evals += 1
        return real_eval(*args, **kwargs)

    real_save = lanes.save_lanes
    delivered_saves = 0

    def fail_publish_marker_save(target_store, data):  # noqa: ANN001
        nonlocal delivered_saves
        lane = data["lanes"]["postpub"]
        if lane.get("status") == lanes.STATUS_DELIVERED:
            delivered_saves += 1
            if delivered_saves == 3:
                raise OSError("injected marker-save failure after final write")
        return real_save(target_store, data)

    monkeypatch.setattr(cli, "_lane_eval", count_eval)
    monkeypatch.setattr(lanes, "save_lanes", fail_publish_marker_save)
    argv = ["lane", "deliver", "--id", "postpub", "--from", "dev", "--head", head]
    assert _run_raw(argv, root) == 2
    finals = _committed_artifacts(store, "postpub")
    assert len(finals) == 1
    assert lanes.validate_delivery_artifact(
        finals[0], lane_id="postpub", head_sha=head, store=store,
    )["artifact_state"] == lanes.ARTIFACT_COMMITTED
    with pytest.raises(lanes.LaneError, match="pending"):
        lanes.validate_delivery_artifact(
            finals[0], lane_id="postpub", head_sha=head, store=store,
            require_live_marker=True,
        )
    assert _run_raw([
        "close", "open", "--id", "pending-close", "--from", "lead",
        "--scope", "release", "--revision", head,
        "--lane-artifact", str(finals[0]),
    ], root) == 0
    assert _run_raw(["close", "check", "--id", "pending-close"], root) == 3

    assert _run_raw(argv, root) == 0
    assert evals == 2  # initial GO plus the terminal post-state rebind; retry adds none
    assert _committed_artifacts(store, "postpub") == finals
    assert lanes.validate_delivery_artifact(
        finals[0], lane_id="postpub", head_sha=head, store=store,
    )["artifact_state"] == lanes.ARTIFACT_COMMITTED


def test_release_close_rejects_final_until_matching_pending_marker_completes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "pending-release-close")
    head = _commit(
        Path(lane["worktree_path"]), "src/core/a.py", "base\npending release close\n",
    )
    store = Store(root)
    real_save = lanes.save_lanes
    delivered_saves = 0

    def fail_marker_checkpoint(target_store, data):  # noqa: ANN001
        nonlocal delivered_saves
        lane_state = data["lanes"]["pending-release-close"]
        if lane_state.get("status") == lanes.STATUS_DELIVERED:
            delivered_saves += 1
            if delivered_saves == 3:
                raise OSError("injected marker-save failure after committed final write")
        return real_save(target_store, data)

    monkeypatch.setattr(lanes, "save_lanes", fail_marker_checkpoint)
    argv = [
        "lane", "deliver", "--id", "pending-release-close", "--from", "dev",
    ]
    assert _run_raw(argv, root) == 2
    finals = _committed_artifacts(store, "pending-release-close")
    assert len(finals) == 1
    assert isinstance(
        lanes.load_lanes(store)["lanes"]["pending-release-close"]["publish_pending"],
        dict,
    )
    pending_marker = lanes.load_lanes(store)["lanes"]["pending-release-close"][
        "publish_pending"
    ]
    Path(pending_marker["prepared_artifact"]).unlink()
    capsys.readouterr()
    assert _run_raw(["lane", "status"], root) == 0
    assert "final_pending_marker" in capsys.readouterr().out
    assert _run_raw(["reset"], root) == 2
    assert _run_raw([
        "lane", "recover", "--id", "pending-release-close",
        "--reason", "must finish the authoritative final",
    ], root) == 3

    assert _run_raw([
        "close", "open", "--id", "pending-release-close-check", "--from", "lead",
        "--scope", "release", "--revision", head,
        "--lane-artifact", str(finals[0]),
    ], root) == 0
    capsys.readouterr()
    assert _run_raw([
        "close", "check", "--id", "pending-release-close-check", "--json",
    ], root) == 3
    close_result = json.loads(capsys.readouterr().out)
    assert "publication is still pending" in close_result["worktree_isolation"]["reason"]

    assert _run_raw(argv, root) == 0
    assert _committed_artifacts(store, "pending-release-close") == finals
    assert _run_raw([
        "close", "check", "--id", "pending-release-close-check",
    ], root) == 0


def test_delivery_cas_rejects_force_reassign_aba(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, "aba")
    store = Store(root)
    original = lanes.load_lanes(store)["lanes"]["aba"]
    real_prepare = lanes.write_prepared_delivery_artifact

    def prepare_then_reassign(*args, **kwargs):  # noqa: ANN002,ANN003
        prepared = real_prepare(*args, **kwargs)
        with store._config_lock():
            data = lanes.load_lanes(store)
            replacement = lanes.new_lane(
                "aba", assignee="dev", assigned_by="lead", assigned_at="replacement",
                domain_id="core", path_subset=["src/core"], base_sha=base,
                target_ref=_branch(root), target_head_at_assign=head,
                epoch_at_assign=store.current_epoch(),
                registry_hash_at_assign=original["registry_hash_at_assign"],
                generation=original["generation"] + 1, release_class=False,
                waiver={"reason": "replacement", "by": "lead", "at": "replacement",
                        "authority": "advisory_only"},
            )
            data["lanes"]["aba"] = replacement
            lanes.save_lanes(store, data)
        return prepared

    monkeypatch.setattr(lanes, "write_prepared_delivery_artifact", prepare_then_reassign)
    assert _run_raw([
        "lane", "deliver", "--id", "aba", "--from", "dev", "--head", head,
    ], root) == 3
    replacement = lanes.load_lanes(store)["lanes"]["aba"]
    assert replacement["status"] == lanes.STATUS_ACTIVE
    assert replacement["instance_id"] != original["instance_id"]
    assert replacement["generation"] == original["generation"] + 1
    assert _committed_artifacts(store, "aba") == []


def test_delivery_cas_does_not_resume_delivered_replacement_generation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, "aba-delivered")
    store = Store(root)
    original = lanes.load_lanes(store)["lanes"]["aba-delivered"]
    real_prepare = lanes.write_prepared_delivery_artifact

    def prepare_then_replace(*args, **kwargs):  # noqa: ANN002,ANN003
        prepared = real_prepare(*args, **kwargs)
        with store._config_lock():
            data = lanes.load_lanes(store)
            replacement = lanes.new_lane(
                "aba-delivered", assignee="dev", assigned_by="lead",
                assigned_at="replacement", domain_id="core",
                path_subset=["src/core"], base_sha=base,
                target_ref=_branch(root), target_head_at_assign=head,
                epoch_at_assign=store.current_epoch(),
                registry_hash_at_assign=original["registry_hash_at_assign"],
                generation=original["generation"] + 1, release_class=False,
                waiver={"reason": "replacement", "by": "lead", "at": "replacement",
                        "authority": "advisory_only"},
            )
            replacement["status"] = lanes.STATUS_DELIVERED
            data["lanes"]["aba-delivered"] = replacement
            lanes.save_lanes(store, data)
        return prepared

    monkeypatch.setattr(lanes, "write_prepared_delivery_artifact", prepare_then_replace)
    assert _run_raw([
        "lane", "deliver", "--id", "aba-delivered", "--from", "dev", "--head", head,
    ], root) == 3
    saved = lanes.load_lanes(store)["lanes"]["aba-delivered"]
    assert saved["instance_id"] != original["instance_id"]
    assert saved["generation"] == original["generation"] + 1
    assert _committed_artifacts(store, "aba-delivered") == []


def test_copied_prepared_artifact_is_never_consumable(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, "copied")
    store = Store(root)

    monkeypatch.setattr(
        lanes, "publish_delivery_artifact",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("stop after prepare")),
    )
    assert _run_raw([
        "lane", "deliver", "--id", "copied", "--from", "dev", "--head", head,
    ], root) == 2
    prepared = _prepared_artifacts(store, "copied")
    assert len(prepared) == 1
    copied = store.dir / "lane-deliveries" / "copied-forged.json"
    shutil.copyfile(prepared[0], copied)
    with pytest.raises(lanes.LaneError, match="prepared"):
        lanes.validate_delivery_artifact(
            copied, lane_id="copied", head_sha=head, store=store,
        )
    assert _run_raw([
        "close", "open", "--id", "prepared-close", "--from", "lead",
        "--scope", "release", "--revision", head,
        "--lane-artifact", str(copied),
    ], root) == 0
    assert _run_raw(["close", "check", "--id", "prepared-close"], root) == 3


@pytest.mark.parametrize("changed_input", ["lane", "active", "config", "registry", "gate", "epoch"])
def test_delivery_cas_rejects_every_cooperating_input_change(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_input: str) -> None:
    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, f"race-{changed_input}")
    store = Store(root)
    real_lock = Store._config_lock
    enters = 0

    @contextlib.contextmanager
    def mutate_on_cas(self, *args, **kwargs):  # noqa: ANN001
        nonlocal enters
        with real_lock(self, *args, **kwargs):
            enters += 1
            if enters == 3:
                if changed_input in {"lane", "active"}:
                    state = lanes.load_lanes(store)
                    if changed_input == "lane":
                        state["lanes"][f"race-{changed_input}"]["notes"] = "changed"
                    else:
                        state["lanes"]["other"] = lanes.new_lane(
                            "other", assignee="dev2", assigned_by="lead", assigned_at="t",
                            domain_id="other", path_subset=["src/other"], base_sha=base,
                            target_ref=_branch(root), target_head_at_assign=head,
                            epoch_at_assign=store.current_epoch(),
                            registry_hash_at_assign="other-registry", release_class=False,
                        )
                    lanes.save_lanes(store, state)
                elif changed_input == "config":
                    cfg = store.load_config()
                    cfg["delivery_race"] = True
                    store.config_path.write_text(json.dumps(cfg), encoding="utf-8")
                elif changed_input == "registry":
                    path = store.dir / "domains.json"
                    registry = json.loads(path.read_text(encoding="utf-8"))
                    registry["domains"]["core"]["title"] = "Changed"
                    path.write_text(json.dumps(registry), encoding="utf-8")
                elif changed_input == "gate":
                    gates.write_gate_state(root, {
                        "schema_version": 1, "required_gates": [],
                        "gates": {"late": {"name": "late", "status": "green"}},
                    })
                else:
                    store.send(
                        sender="lead", recipient="lead", body="barrier",
                        meta={"barrier": {"version": 1, "scope": "global", "type": "release"}},
                    )
            yield

    monkeypatch.setattr(Store, "_config_lock", mutate_on_cas)
    assert _run_raw([
        "lane", "deliver", "--id", f"race-{changed_input}", "--from", "dev",
        "--head", head,
    ], root) == 3
    saved = lanes.load_lanes(store)["lanes"][f"race-{changed_input}"]
    assert saved["status"] == lanes.STATUS_ACTIVE
    assert _committed_artifacts(store, f"race-{changed_input}") == []


def test_delivery_rechecks_target_ref_after_evaluation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, "target-race")
    target_ref = _branch(root)
    real_resolve = cli._lane_resolve
    target_reads = 0

    def moving_target(store, ref):  # noqa: ANN001
        nonlocal target_reads
        if ref == target_ref:
            target_reads += 1
            if target_reads >= 2:
                return base
        return real_resolve(store, ref)

    monkeypatch.setattr(cli, "_lane_resolve", moving_target)
    assert _run_raw([
        "lane", "deliver", "--id", "target-race", "--from", "dev", "--head", head,
    ], root) == 3
    assert lanes.load_lanes(Store(root))["lanes"]["target-race"]["status"] == lanes.STATUS_ACTIVE
    assert _committed_artifacts(Store(root), "target-race") == []


def test_delivery_teardown_failure_is_retryable_without_reevaluation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "teardown-fail")
    wt = Path(lane["worktree_path"])
    _commit(wt, "src/core/a.py", "base\nteardown\n")
    store = Store(root)
    real_eval = cli._lane_eval
    evals = 0

    def count_eval(*args, **kwargs):  # noqa: ANN002,ANN003
        nonlocal evals
        evals += 1
        return real_eval(*args, **kwargs)

    real_git_write = cli._git_write

    def fail_remove(git_root, argv, **kwargs):  # noqa: ANN001
        if argv[:2] == ["worktree", "remove"]:
            return 1, "", "injected teardown failure"
        return real_git_write(git_root, argv, **kwargs)

    monkeypatch.setattr(cli, "_lane_eval", count_eval)
    monkeypatch.setattr(cli, "_git_write", fail_remove)
    argv = ["lane", "deliver", "--id", "teardown-fail", "--from", "dev"]
    assert _run_raw(argv, root) == 0
    pending = lanes.load_lanes(store)["lanes"]["teardown-fail"]
    assert pending["cleanup_pending"] is True
    assert pending["worktree_state"] == lanes.STATUS_CLEANUP_FAILED
    assert wt.exists()
    capsys.readouterr()
    assert _run_raw(["lane", "status"], root) == 0
    assert "cleanup_pending" in capsys.readouterr().out
    assert _run_raw(["reset"], root) == 2

    monkeypatch.setattr(cli, "_git_write", real_git_write)
    assert _run_raw(argv, root) == 0
    assert evals == 2  # initial GO plus the terminal post-state rebind; retry adds none
    complete = lanes.load_lanes(store)["lanes"]["teardown-fail"]
    assert complete["cleanup_pending"] is False
    assert not wt.exists()


def test_delivery_recovers_when_teardown_succeeds_but_cleanup_save_fails(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "cleanup-save")
    wt = Path(lane["worktree_path"])
    _commit(wt, "src/core/a.py", "base\ncleanup save\n")
    store = Store(root)
    real_eval = cli._lane_eval
    evals = 0

    def count_eval(*args, **kwargs):  # noqa: ANN002,ANN003
        nonlocal evals
        evals += 1
        return real_eval(*args, **kwargs)

    real_save = lanes.save_lanes
    delivered_saves = 0

    def fail_cleanup_checkpoint(target_store, data):  # noqa: ANN001
        nonlocal delivered_saves
        lane_state = data["lanes"]["cleanup-save"]
        if lane_state.get("status") == lanes.STATUS_DELIVERED:
            delivered_saves += 1
            if delivered_saves == 4:
                raise OSError("injected cleanup checkpoint failure")
        return real_save(target_store, data)

    monkeypatch.setattr(cli, "_lane_eval", count_eval)
    monkeypatch.setattr(lanes, "save_lanes", fail_cleanup_checkpoint)
    argv = ["lane", "deliver", "--id", "cleanup-save", "--from", "dev"]
    assert _run_raw(argv, root) == 2
    assert not wt.exists()
    pending = lanes.load_lanes(store)["lanes"]["cleanup-save"]
    assert pending["cleanup_pending"] is True

    assert _run_raw(argv, root) == 0
    assert evals == 2  # initial GO plus the terminal post-state rebind; retry adds none
    assert lanes.load_lanes(store)["lanes"]["cleanup-save"]["cleanup_pending"] is False


def test_concurrent_delivery_commits_one_artifact_and_retry_never_reevaluates(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, "concurrent")
    store = Store(root)
    barrier = threading.Barrier(2)
    real_eval = cli._lane_eval
    evals = 0
    eval_lock = threading.Lock()

    def rendezvous_eval(*args, **kwargs):  # noqa: ANN002,ANN003
        nonlocal evals
        result = real_eval(*args, **kwargs)
        with eval_lock:
            evals += 1
            rendezvous = evals <= 2
        if rendezvous:
            barrier.wait(timeout=20)
        return result

    monkeypatch.setattr(cli, "_lane_eval", rendezvous_eval)
    argv = ["lane", "deliver", "--id", "concurrent", "--from", "dev", "--head", head]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _i: _run_raw(argv, root), range(2)))

    assert results == [0, 0]
    finals = _committed_artifacts(store, "concurrent")
    assert len(finals) == 1
    state = lanes.load_lanes(store)["lanes"]["concurrent"]
    assert state["delivery_artifact"] == str(finals[0])
    assert _run_raw(argv, root) == 0
    assert evals == 3  # two initial evaluations; only the winner rebinds, retry adds none
    assert _committed_artifacts(store, "concurrent") == finals


def test_concurrent_delivery_loser_reports_winner_head(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    first_head = _advisory_delivery_fixture(root, base, "concurrent-head")
    second_head = _commit(root, "src/core/a.py", "base\nsecond candidate\n")
    assert first_head != second_head
    barrier = threading.Barrier(2)
    real_eval = cli._lane_eval
    evals = 0
    eval_lock = threading.Lock()

    def rendezvous_eval(*args, **kwargs):  # noqa: ANN002,ANN003
        nonlocal evals
        result = real_eval(*args, **kwargs)
        with eval_lock:
            evals += 1
            rendezvous = evals <= 2
        if rendezvous:
            barrier.wait(timeout=20)
        return result

    output: list[str] = []
    output_lock = threading.Lock()

    def capture_print(*args, **_kwargs):  # noqa: ANN002
        with output_lock:
            output.append(" ".join(str(item) for item in args))

    monkeypatch.setattr(cli, "_lane_eval", rendezvous_eval)
    monkeypatch.setattr(builtins, "print", capture_print)
    argvs = [
        ["lane", "deliver", "--id", "concurrent-head", "--from", "dev",
         "--head", first_head],
        ["lane", "deliver", "--id", "concurrent-head", "--from", "dev",
         "--head", second_head],
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda argv: _run_raw(argv, root), argvs))

    assert sorted(results) == [0, 3]
    winner = lanes.load_lanes(Store(root))["lanes"]["concurrent-head"]["delivered_head"]
    delivery_lines = [line for line in output if line.startswith("delivered lane concurrent-head")]
    assert len(delivery_lines) == 1
    assert winner[:12] in delivery_lines[0]
    assert winner[:12] in capsys.readouterr().err
    assert len(_committed_artifacts(Store(root), "concurrent-head")) == 1


def test_delivery_keeps_git_artifact_io_and_teardown_outside_global_lock(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "lock-scope")
    wt = Path(lane["worktree_path"])
    _commit(wt, "src/core/a.py", "base\nlock scope\n")
    depth = 0
    real_lock = Store._config_lock

    @contextlib.contextmanager
    def tracked_lock(self, *args, **kwargs):  # noqa: ANN001
        nonlocal depth
        with real_lock(self, *args, **kwargs):
            depth += 1
            try:
                yield
            finally:
                depth -= 1

    monkeypatch.setattr(Store, "_config_lock", tracked_lock)

    def outside(obj, name):  # noqa: ANN001
        real = getattr(obj, name)

        def checked(*args, **kwargs):  # noqa: ANN002,ANN003
            assert depth == 0, f"{name} ran under the global config lock"
            return real(*args, **kwargs)

        monkeypatch.setattr(obj, name, checked)

    for obj, name in (
        (cli, "_lane_candidate"),
        (cli, "_lane_eval"),
        (cli, "_verify_lane_worktree"),
        (cli, "_lane_worktree_idle"),
        (cli, "_git_write"),
        (lanes, "write_prepared_delivery_artifact"),
        (lanes, "publish_delivery_artifact"),
        (lanes, "existing_committed_delivery_artifact"),
        (lanes, "validate_prepared_delivery_artifact"),
        (lanes, "validate_delivery_artifact"),
        (lanes, "cooperating_input_fingerprint"),
    ):
        outside(obj, name)

    real_save = lanes.save_lanes

    def save_under_lock(*args, **kwargs):  # noqa: ANN002,ANN003
        assert depth > 0
        return real_save(*args, **kwargs)

    monkeypatch.setattr(lanes, "save_lanes", save_under_lock)
    assert _run_raw([
        "lane", "deliver", "--id", "lock-scope", "--from", "dev",
    ], root) == 0
    assert not wt.exists()
