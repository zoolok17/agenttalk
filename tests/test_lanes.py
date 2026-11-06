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
import errno
import builtins
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

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

    Store(root).set_operator_facing("dev2")
    assert _run_raw([
        "lane", "assign", "--id", "operator-denied", "--from", "dev2",
        "--assignee", "dev", *common,
    ], root) == 2
    assert "release-class" in capsys.readouterr().err.lower()
    assert "operator-denied" not in lanes.load_lanes(Store(root))["lanes"]

    assert _run_raw([
        "lane", "assign", "--id", "advisory", "--from", "dev",
        "--assignee", "dev", "--advisory", *common,
    ], root) == 0
    advisory_lane = lanes.load_lanes(Store(root))["lanes"]["advisory"]
    assert advisory_lane["release_class"] is False
    assert advisory_lane["worktree_waiver_authority"] == "advisory_only"
    head = _commit(root, "src/core/a.py", "base\nadvisory\n")
    assert _run_raw([
        "lane", "deliver", "--id", "advisory", "--from", "dev", "--head", head,
    ], root) == 0
    artifact = next((Store(root).dir / "lane-deliveries").glob("advisory-*.json"))
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["isolation_status"] == "advisory_unisolated"
    assert payload["release_class"] is False
    assert payload["worktree_waiver_authority"] == "advisory_only"
    assert lanes.validate_delivery_artifact(
        artifact, lane_id="advisory", head_sha=head, store=Store(root),
    )["release_class"] is False
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

    authority_cases = (
        ("sole-lead", "lead", "sole_lead"),
        ("operator", "dev2", "operator_facing"),
    )
    for name, waived_by, authority in authority_cases:
        if authority == "operator_facing":
            Store(root).set_operator_facing(waived_by)
        legacy = dict(payload)
        legacy["integrity_version"] = lanes.LEGACY_INTEGRITY_VERSION
        legacy["evaluation_snapshot"] = dict(legacy["evaluation_snapshot"])
        legacy["evaluation_snapshot"]["evaluation_version"] = \
            lanes.LEGACY_EVALUATION_VERSION
        legacy["isolation_status"] = "waived"
        legacy["worktree_waiver_authority"] = authority
        legacy["worktree_waived_by"] = waived_by
        legacy["integrity_token"] = lanes.compute_integrity_token(Store(root), legacy)
        legacy_path = Store(root).dir / "lane-deliveries" / f"legacy-{name}.json"
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
        with pytest.raises(lanes.LaneError, match="legacy waived"):
            lanes.validate_delivery_artifact(
                legacy_path, lane_id="advisory", head_sha=head, store=Store(root),
                require_isolation=True,
            )
        close_id = f"legacy-{name}-close"
        assert _run_raw([
            "close", "open", "--id", close_id, "--from", "lead",
            "--scope", "release", "--revision", head,
            "--lane-artifact", str(legacy_path),
        ], root) == 0
        assert _run_raw(["close", "check", "--id", close_id], root) == 3

        if authority == "sole_lead":
            Store(root).set_role("dev2", "lead")
        else:
            Store(root).set_operator_facing("lead")
        with pytest.raises(lanes.LaneError, match="legacy waived"):
            lanes.validate_delivery_artifact(
                legacy_path, lane_id="advisory", head_sha=head, store=Store(root),
                require_isolation=True,
            )
        assert _run_raw(["close", "check", "--id", close_id], root) == 3

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


def test_historical_v2_verified_compatibility_and_v3_release_class_integrity(
        tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "integrity-versions")
    head = _commit(
        Path(lane["worktree_path"]), "src/core/a.py", "base\nintegrity versions\n",
    )
    assert _run_raw([
        "lane", "deliver", "--id", "integrity-versions", "--from", "dev",
    ], root) == 0
    store = Store(root)
    artifact = _committed_artifacts(store, "integrity-versions")[0]
    v3 = json.loads(artifact.read_text(encoding="utf-8"))
    assert v3["integrity_version"] == lanes.INTEGRITY_VERSION
    assert v3["release_class"] is True

    tampered = json.loads(json.dumps(v3))
    tampered["release_class"] = False
    tampered_path = store.dir / "lane-deliveries" / "v3-release-class-tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(lanes.LaneError, match="integrity token"):
        lanes.validate_delivery_artifact(
            tampered_path, lane_id="integrity-versions", head_sha=head,
            store=store, require_isolation=True,
        )

    historical = json.loads(json.dumps(v3))
    historical["integrity_version"] = lanes.LEGACY_INTEGRITY_VERSION
    for key in (
        "artifact_state", "transaction_id", "lane_instance_id", "lane_generation",
        "release_class",
    ):
        historical.pop(key, None)
    historical["evaluation_snapshot"]["evaluation_version"] = \
        lanes.LEGACY_EVALUATION_VERSION
    for key in (
        "lane_instance_id", "lane_generation", "config_digest", "cooperating_digest",
    ):
        historical["evaluation_snapshot"].pop(key, None)
    historical["integrity_token"] = lanes.compute_integrity_token(store, historical)
    historical_path = store.dir / "lane-deliveries" / "historical-v2-verified.json"
    historical_path.write_text(json.dumps(historical), encoding="utf-8")

    validated = lanes.validate_delivery_artifact(
        historical_path, lane_id="integrity-versions", head_sha=head,
        store=store, require_isolation=True,
    )
    assert validated["integrity_version"] == lanes.LEGACY_INTEGRITY_VERSION
    assert "release_class" not in validated


@pytest.mark.parametrize(
    ("waived_by", "authority"),
    (("lead", "sole_lead"), ("dev2", "operator_facing")),
)
def test_cli_deliver_rejects_forged_no_worktree_waiver(
        tmp_path: Path, waived_by: str, authority: str) -> None:
    root, base = _repo(tmp_path)
    br = _branch(root)
    assert _run([
        "lane", "assign", "--id", "forged", "--from", "lead",
        "--assignee", "dev", "--domain", "core", "--base", base,
        "--target", br, "--path", "src/core",
    ], root) == 0
    store = Store(root)
    if authority == "operator_facing":
        store.set_operator_facing(waived_by)
    state = lanes.load_lanes(store)
    state["lanes"]["forged"]["release_class"] = True
    state["lanes"]["forged"]["worktree_waived_by"] = waived_by
    state["lanes"]["forged"]["worktree_waiver_authority"] = authority
    lanes.save_lanes(store, state)
    head = _commit(root, "src/core/a.py", "base\nchange\n")

    assert _run_raw([
        "lane", "deliver", "--id", "forged", "--from", "dev", "--head", head,
    ], root) == 3
    assert lanes.load_lanes(store)["lanes"]["forged"]["status"] == lanes.STATUS_ACTIVE
    deliveries = store.dir / "lane-deliveries"
    assert not deliveries.exists() or not list(deliveries.glob("*.json"))

    if authority == "sole_lead":
        store.set_role("dev2", "lead")
    else:
        store.set_operator_facing("lead")
    assert _run_raw([
        "lane", "deliver", "--id", "forged", "--from", "dev", "--head", head,
    ], root) == 3
    assert lanes.load_lanes(store)["lanes"]["forged"]["status"] == lanes.STATUS_ACTIVE


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


def test_abandon_of_a_lane_that_was_never_created_says_so_and_fails(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """#615: abandoning a lane id with no record used to print "abandoned lane X" and exit 0. A mistyped id
    must not look like a success; it is refused the way lane check, deliver and workspace refuse an unknown
    lane (exit 2), and nothing is touched."""
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "real")
    store = Store(root)
    before = lanes.load_lanes(store)

    assert _run_raw(["lane", "abandon", "--id", "ghost"], root) == 2

    captured = capsys.readouterr()
    assert "abandoned" not in captured.out
    assert "no lane 'ghost'" in captured.err and "nothing was changed" in captured.err
    assert lanes.load_lanes(store) == before
    assert "ghost" not in lanes.load_lanes(store)["lanes"]


def test_abandon_of_an_unknown_lane_leaves_a_branch_of_that_name_alone(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """#615: with --delete-branch the unknown-lane case must not delete anything either."""
    root, base = _repo(tmp_path)
    assert _git_rc(root, "branch", "lane/ghost", base).returncode == 0

    assert _run_raw(["lane", "abandon", "--id", "ghost", "--delete-branch", "--target", _branch(root)], root) == 2

    captured = capsys.readouterr()
    assert "abandoned" not in captured.out and "branch deleted" not in captured.out
    assert "no lane 'ghost'" in captured.err
    assert _git_rc(root, "rev-parse", "--verify", "refs/heads/lane/ghost^{commit}").returncode == 0


def test_abandon_also_exits_2_and_changes_nothing_for_a_malformed_id_or_an_unreadable_lanes_file(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The other early refusals `lane abandon --help` names for exit 2 ("A refusal at the start changes nothing")."""
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "real")
    path = lanes.lanes_path(Store(root))

    assert _run_raw(["lane", "abandon", "--id", "bad/id"], root) == 2
    assert lanes.load_lanes(Store(root))["lanes"]["real"]["status"] == lanes.STATUS_ACTIVE

    path.write_text("{not json", encoding="utf-8")
    assert _run_raw(["lane", "abandon", "--id", "real"], root) == 2
    assert path.read_text(encoding="utf-8") == "{not json"
    assert "abandoned" not in capsys.readouterr().out


def test_abandon_that_fails_after_the_cleanup_exits_2_and_the_help_says_what_a_retry_does(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """Zteam's P2 on PR 642: the worktree is removed first and the lane record saved after it, so a failed save
    exits 2 with the worktree gone and the record still active. The order is unchanged; the help must not promise
    "nothing was changed" for exit 2 as a whole. A retry does not finish the cleanup (PR 642, round 4): once the
    save works again it records the lane as abandoned with the worktree cleanup still marked pending; while the save
    still fails it fails the same way (round 5). The help says exactly that."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "savefails")
    worktree = Path(lane["worktree_path"])
    store = Store(root)
    real_save = lanes.save_lanes

    def failing_save(*args, **kwargs):
        raise OSError("disk full (injected)")

    monkeypatch.setattr(lanes, "save_lanes", failing_save)
    assert _run_raw(["lane", "abandon", "--id", "savefails"], root) == 2

    captured = capsys.readouterr()
    assert "abandoned lane" not in captured.out
    assert not worktree.exists()                                     # the cleanup had already happened
    assert lanes.load_lanes(store)["lanes"]["savefails"]["status"] == lanes.STATUS_ACTIVE

    with pytest.raises(SystemExit):
        cli.main(["lane", "abandon", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "Exit 2: nothing was changed" not in text                 # not a promise for the whole exit code
    assert "A refusal at the start changes nothing" in text
    assert "can leave the worktree gone while the lane still shows its old status" in text
    assert "While that problem lasts, running the command again fails the same way" in text
    assert "once it is resolved, a successful retry marks the lane abandoned but leaves its worktree cleanup " \
           "marked pending" in " ".join(text.split())
    assert "finishes it" not in text                                 # no completion promise
    assert "--acknowledge-missing-worktree" in text                  # #643: where to go when the worktree is gone

    # while the save still fails, a second call fails the same way and changes nothing more
    assert _run_raw(["lane", "abandon", "--id", "savefails"], root) == 2
    assert "abandoned lane" not in capsys.readouterr().out
    assert not worktree.exists()
    assert lanes.load_lanes(store)["lanes"]["savefails"]["status"] == lanes.STATUS_ACTIVE
    assert lanes.load_lanes(store)["lanes"]["savefails"]["worktree_state"] == lanes.STATUS_ACTIVE

    # the save works again: now a retry succeeds, with the cleanup still marked pending
    monkeypatch.setattr(lanes, "save_lanes", real_save)
    assert _run_raw(["lane", "abandon", "--id", "savefails"], root) == 0
    retried = lanes.load_lanes(store)["lanes"]["savefails"]
    assert retried["status"] == lanes.STATUS_ABANDONED
    assert retried["worktree_state"] == lanes.STATUS_CLEANUP_PENDING   # what the help says: still marked pending
    assert retried["worktree_cleanup_error"]                          # with the reason it could not be completed
    assert not worktree.exists()


ACK = "--acknowledge-missing-worktree"


def _fail_one_save_then_recover(root: Path, lane_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`lane abandon` removes the worktree, then its save fails (exit 2); the save works again afterwards."""
    real_save = lanes.save_lanes

    def failing_save(*args, **kwargs):
        raise OSError("disk full (injected)")

    monkeypatch.setattr(lanes, "save_lanes", failing_save)
    assert _run_raw(["lane", "abandon", "--id", lane_id], root) == 2
    monkeypatch.setattr(lanes, "save_lanes", real_save)


def _branch_exists(root: Path, name: str) -> bool:
    return _git_rc(root, "rev-parse", "--verify", f"refs/heads/{name}^{{commit}}").returncode == 0


def _branch_tips(root: Path) -> dict[str, str]:
    return dict(line.split()[::-1] for line in _git(root, "for-each-ref", "--format=%(objectname) %(refname)",
                                                    "refs/heads/").splitlines())


def test_acknowledging_a_missing_worktree_finishes_the_record_and_never_touches_a_branch(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """#643, the acceptance sequence: a failed save, the acknowledgement, then repeated abandon and gc. The record is
    finished (as acknowledged, not verified), the branches are exactly as they were, and nothing is deleted."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "ack")
    store = Store(root)
    _fail_one_save_then_recover(root, "ack", monkeypatch)
    assert not Path(lane["worktree_path"]).exists()
    tips = _branch_tips(root)
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "ack", ACK], root) == 0

    out = capsys.readouterr().out
    assert "acknowledged" in out and "not verified" in out
    record = lanes.load_lanes(store)["lanes"]["ack"]
    assert record["status"] == lanes.STATUS_ABANDONED
    assert record["worktree_state"] == lanes.STATUS_CLEANUP_ACKNOWLEDGED       # not "abandoned": nothing was verified
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", record["worktree_cleanup_acknowledged_at"])
    assert not record.get("worktree_cleanup_error")
    assert _branch_tips(root) == tips

    # repeated abandon, plain and with --delete-branch: the record stays as it is, the branch stays
    assert _run_raw(["lane", "abandon", "--id", "ack"], root) == 0
    assert lanes.load_lanes(store)["lanes"]["ack"] == record
    capsys.readouterr()
    assert _run_raw(["lane", "abandon", "--id", "ack", "--delete-branch", "--target", _branch(root)], root) == 0
    captured = capsys.readouterr()
    assert "branch deleted" not in captured.out and "not deleted" in captured.err and "acknowledged" in captured.err
    assert lanes.load_lanes(store)["lanes"]["ack"] == record
    assert _branch_tips(root) == tips

    # gc, dry run and delete: the branch is never proven safe by an acknowledgement
    for argv in (["lane", "gc", "--json"], ["lane", "gc", "--delete", "--json"]):
        assert _run_raw(argv, root) == 0
        item = next(i for i in json.loads(capsys.readouterr().out)["items"] if i["lane_id"] == "ack")
        assert item["branch_delete_safe"] is False and "acknowledged" in item["reason"]
    assert lanes.load_lanes(store)["lanes"]["ack"] == record
    assert _branch_tips(root) == tips


def test_acknowledging_after_a_worktree_was_moved_and_switched_deletes_and_removes_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """#643: `git worktree move` then `git checkout -b other` leaves a live worktree on another branch and nothing that
    names the lane. The person acknowledges the old path is gone; the live worktree and BOTH branches stay."""
    (tmp_path / "store").mkdir()
    root, base = _repo(tmp_path / "store")
    lane = _assign_worktree(root, base, "switched")
    old = Path(lane["worktree_path"])
    live = tmp_path / "live" / "elsewhere"
    live.parent.mkdir()
    assert _git_rc(root, "worktree", "move", str(old), str(live)).returncode == 0
    assert _git_rc(live, "checkout", "-q", "-b", "other").returncode == 0
    tips = _branch_tips(root)

    assert _run_raw(["lane", "abandon", "--id", "switched", ACK], root) == 0
    record = lanes.load_lanes(Store(root))["lanes"]["switched"]
    assert record["worktree_state"] == lanes.STATUS_CLEANUP_ACKNOWLEDGED

    for argv in (["lane", "abandon", "--id", "switched"],
                 ["lane", "abandon", "--id", "switched", "--delete-branch", "--target", _branch(root)],
                 ["lane", "gc", "--delete", "--json"]):
        assert _run_raw(argv, root) == 0
    capsys.readouterr()
    assert lanes.load_lanes(Store(root))["lanes"]["switched"] == record
    assert _branch_tips(root) == tips and "refs/heads/lane/switched" in tips and "refs/heads/other" in tips
    assert live.exists() and _git(live, "branch", "--show-current").strip() == "other"


def test_a_second_failed_save_leaves_the_acknowledgement_unrecorded_and_the_next_one_records_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "twice")
    store = Store(root)
    real_save = lanes.save_lanes
    _fail_one_save_then_recover(root, "twice", monkeypatch)
    before = lanes.load_lanes(store)
    capsys.readouterr()

    def failing_save(*args, **kwargs):
        raise OSError("disk full again (injected)")

    monkeypatch.setattr(lanes, "save_lanes", failing_save)
    assert _run_raw(["lane", "abandon", "--id", "twice", ACK], root) == 2
    assert "acknowledged" not in capsys.readouterr().out
    assert lanes.load_lanes(store) == before                          # nothing claimed, nothing changed

    monkeypatch.setattr(lanes, "save_lanes", real_save)
    assert _run_raw(["lane", "abandon", "--id", "twice", ACK], root) == 0
    assert lanes.load_lanes(store)["lanes"]["twice"]["worktree_state"] == lanes.STATUS_CLEANUP_ACKNOWLEDGED


def test_the_acknowledgement_cannot_be_combined_with_delete_branch(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "combo")
    _fail_one_save_then_recover(root, "combo", monkeypatch)
    store = Store(root)
    before, tips = lanes.load_lanes(store), _branch_tips(root)
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "combo", ACK, "--delete-branch"], root) == 2

    captured = capsys.readouterr()
    assert "--delete-branch" in captured.err and ACK in captured.err and captured.out == ""
    assert lanes.load_lanes(store) == before and _branch_tips(root) == tips


def test_the_refused_combination_does_not_open_the_store_at_all(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """#643 round 6: the combination is refused before anything is read or written, so a store that cannot even be
    opened (or a configuration that cannot be read) does not hide the message."""
    def no_store(*args, **kwargs):
        raise AssertionError("the store was opened")

    monkeypatch.setattr(cli, "_get_store", no_store)

    assert cli.main(["--root", str(tmp_path), "lane", "abandon", "--id", "any", ACK, "--delete-branch"]) == 2

    err = capsys.readouterr().err
    assert "--delete-branch" in err and ACK in err and "Nothing was changed" in err


def test_the_acknowledgement_is_refused_while_the_worktree_folder_still_exists(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A present folder is the one thing the option can see; it refuses (exit 2) and changes nothing."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "present")
    store = Store(root)
    before = lanes.load_lanes(store)

    assert _run_raw(["lane", "abandon", "--id", "present", ACK], root) == 2

    assert "still exists" in capsys.readouterr().err
    assert lanes.load_lanes(store) == before and Path(lane["worktree_path"]).exists()


def test_the_acknowledgement_is_refused_while_a_launch_still_uses_the_lane(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "busyack")
    store = Store(root)
    store.write_launch_request({"request_id": "lr-busy-ack", "state": "queued", "lane_id": "busyack"})
    shutil.rmtree(lane["worktree_path"])                              # the folder is gone, a launch still names it
    before = lanes.load_lanes(store)
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "busyack", ACK], root) == 2

    assert "launch" in capsys.readouterr().err
    assert lanes.load_lanes(store) == before


def test_the_acknowledgement_is_refused_while_a_launched_marker_names_a_moved_worktree(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """PR 654 round 9: a `launched` marker is a running launch. The worktree was moved, not deleted, so the folder at
    the stored path is gone and only the launch check stands in the way."""
    (tmp_path / "store").mkdir()
    root, base = _repo(tmp_path / "store")
    lane = _assign_worktree(root, base, "launchedack")
    store = Store(root)
    store.write_launch_request({"request_id": "lr-launched-ack", "state": "launched", "lane_id": "launchedack"})
    live = tmp_path / "live" / "elsewhere"
    live.parent.mkdir()
    assert _git_rc(root, "worktree", "move", lane["worktree_path"], str(live)).returncode == 0
    before = lanes.load_lanes(store)
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "launchedack", ACK], root) == 2

    err = capsys.readouterr().err
    assert "active or pending launch" in err and "'lr-launched-ack'" in err and "'launched'" in err
    assert lanes.load_lanes(store) == before and live.exists()


@pytest.mark.parametrize("section", ["agents", "ephemeral_reviewers"])
def test_the_acknowledgement_is_refused_while_the_supervisors_own_record_names_the_worktree(
        tmp_path: Path, capsys: pytest.CaptureFixture, section: str) -> None:
    """PR 654 round 9: the supervisor writes `supervisor-state.json` straight under the store folder; the guard used
    to look in `state/`, so a running agent or ephemeral reviewer in the lane's folder went unseen."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "supack")
    store = Store(root)
    record = {"phase": "launched", "cwd": lane["worktree_path"]}
    state = ({"agents": {"claude-dev": record}} if section == "agents"
             else {"agents": {}, "ephemeral_reviewers": {"active": {"lr-sup": record}}})
    (store.dir / "supervisor-state.json").write_text(json.dumps(state), encoding="utf-8")
    shutil.rmtree(lane["worktree_path"])
    before = lanes.load_lanes(store)
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "supack", ACK], root) == 2

    err = capsys.readouterr().err
    record_name = "claude-dev" if section == "agents" else "lr-sup"
    assert "active or pending launch" in err and f"supervisor-state.json, {section}, record '{record_name}'" in err
    assert lanes.load_lanes(store) == before


def test_the_acknowledgement_says_it_could_not_establish_idleness_when_the_supervisor_file_is_unreadable(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """PR 654 round 9: an unreadable record is not a launch that was found, and it is never taken as idle."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "unreadable")
    store = Store(root)
    (store.dir / "supervisor-state.json").write_text("{not json", encoding="utf-8")
    shutil.rmtree(lane["worktree_path"])
    before = lanes.load_lanes(store)
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "unreadable", ACK], root) == 2

    err = capsys.readouterr().err
    assert "could not be established" in err and "active or pending launch" not in err
    assert "supervisor-state.json" in err and "was not removed" in err
    assert lanes.load_lanes(store) == before


def _held_back_or_refused(root: Path, lane: dict, lane_id: str, action: str, capsys: pytest.CaptureFixture,
                          inspect: str) -> None:
    """Shared check for a record that cannot be read: plain `lane abandon` keeps the worktree and leaves the cleanup
    pending; the acknowledgement (after the folder is gone) is refused and changes nothing. Either way the message
    names what to inspect (`inspect`) and says the worktree was not removed."""
    store = Store(root)
    wt = Path(lane["worktree_path"])
    capsys.readouterr()
    if action == "abandon":
        assert _run_raw(["lane", "abandon", "--id", lane_id], root) == 0
        record = lanes.load_lanes(store)["lanes"][lane_id]
        assert record["worktree_state"] == lanes.STATUS_CLEANUP_PENDING and wt.exists()
        error = record["worktree_cleanup_error"]
        assert "could not be established" in error and inspect in error and "was not removed" in error
    else:
        shutil.rmtree(wt)
        before = lanes.load_lanes(store)
        capsys.readouterr()
        assert _run_raw(["lane", "abandon", "--id", lane_id, ACK], root) == 2
        err = capsys.readouterr().err
        assert "could not be established" in err and inspect in err and "was not removed" in err
        assert lanes.load_lanes(store) == before


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize("content", ["{not json", "", "[]"])
def test_a_launch_marker_that_cannot_be_read_never_means_idle(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str, content: str) -> None:
    """PR 654 round 10: the store's listing silently drops a marker file it cannot parse, so the check used to see
    "no launch". Any marker file that cannot be read is now "could not be established"."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "badmarker")
    store = Store(root)
    store.write_launch_request({"request_id": "lr-bad", "state": "launched", "lane_id": "badmarker"})
    (store.launch_requests_dir / "lr-bad.json").write_text(content, encoding="utf-8")
    assert store.list_launch_requests() == []                         # the listing really does drop it

    _held_back_or_refused(root, lane, "badmarker", action, capsys, "lr-bad.json")


_case_insensitive_names = pytest.mark.skipif(os.name != "nt", reason="Windows names are not case-sensitive")


def _rename_suffix(path: Path, suffix: str) -> Path:
    """Rename only the suffix of a file (`x.json` -> `x.JSON`) and return the new path, as the folder lists it."""
    target = path.with_suffix(suffix)
    os.replace(path, target)
    assert target.name in os.listdir(path.parent)                       # the entry really has the new spelling
    return target


@_case_insensitive_names
@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize("suffix", [".JSON", ".Json"])
def test_a_launch_file_with_an_upper_case_suffix_cannot_be_established_on_a_case_insensitive_system(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str, suffix: str) -> None:
    """654-R31-C1 / 35: on Windows `lr-live.JSON` is the very file the store reads as `lr-live.json`. The scan used to
    skip it (a live launch of the lane read as idle); it then read it through the id. It is now judged by the entry that
    was found: not a marker the store wrote, so "could not be established", named, whatever the file holds."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "casing")
    store = Store(root)
    _live_ephemeral_launch(store, "lr-live")
    store.write_launch_request({"request_id": "lr-live", "state": "launched", "lane_id": "casing"})
    _rename_suffix(store.launch_requests_dir / "lr-live.json", suffix)
    assert store.read_launch_request("lr-live") is not None             # the store still reads it: same file

    _held_back_or_refused(root, lane, "casing", action, capsys, f"lr-live{suffix}")


@_case_insensitive_names
@pytest.mark.parametrize("suffix", [".JSON", ".Json"])
def test_an_upper_case_suffix_entry_that_is_not_a_file_is_damage_on_windows(tmp_path: Path, suffix: str) -> None:
    root, _base = _repo(tmp_path)
    store = Store(root)
    store.launch_requests_dir.mkdir(parents=True, exist_ok=True)
    (store.launch_requests_dir / f"lr-dir{suffix}").mkdir()

    markers, damaged = cli._lane_launch_markers(store)

    assert markers == [] and damaged == f"lr-dir{suffix}"


def _folder_ignores_case(folder: Path) -> bool:
    probe = folder / "case-probe.json"
    probe.write_text("{}", encoding="utf-8")
    try:
        return (folder / "CASE-PROBE.JSON").exists()
    finally:
        probe.unlink()


def test_a_stray_upper_case_suffix_file_is_damage_on_every_volume(tmp_path: Path) -> None:
    """654-R32-C1 / 35: a case variant of the suffix is "could not be established" whatever the volume does and whatever
    the file holds: it is judged by the entry found, never by re-reading `<id>.json`."""
    root, _base = _repo(tmp_path)
    store = Store(root)
    store.launch_requests_dir.mkdir(parents=True, exist_ok=True)
    (store.launch_requests_dir / "lr-stray.JSON").write_text("{not json", encoding="utf-8")

    assert cli._lane_launch_markers(store) == ([], "lr-stray.JSON")


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_a_case_variant_marker_beside_the_exact_one_is_judged_by_its_own_name(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str) -> None:
    """GitHub's reviewer on #654 (cee0a16d): with `r.json` (terminal) and `r.JSON` (the live `launched` marker of the
    lane) side by side, the scan read `r.json` twice and saw neither a launch nor damage, so the acknowledgement could
    abandon the live lane. Only where the volume tells names apart; elsewhere the two cannot exist side by side."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "besides")
    store = Store(root)
    store.launch_requests_dir.mkdir(parents=True, exist_ok=True)
    folder = store.launch_requests_dir
    (folder / "r.json").write_text(json.dumps({"request_id": "r", "state": "archived"}), encoding="utf-8")
    if (folder / "r.JSON").exists():
        pytest.skip("this volume ignores case: the two names are one file (covered by the fake-listing test)")
    (folder / "r.JSON").write_text(
        json.dumps({"request_id": "r", "state": "launched", "lane_id": "besides"}), encoding="utf-8")

    _held_back_or_refused(root, lane, "besides", action, capsys, "r.JSON")


def test_a_case_variant_is_never_read_through_the_id_whatever_the_volume(tmp_path: Path) -> None:
    """The same rule as a pure function with a fake listing, so it runs on every volume: the exact `r.json` is read
    once, by its id; `r.JSON` is named as damage and `read_launch_request` is never asked for it."""
    folder = tmp_path / "launch-requests"
    folder.mkdir()
    (folder / "r.json").write_text("{}", encoding="utf-8")
    asked = []

    class Listing(type(folder)):
        def iterdir(self):
            yield self / "r.json"
            yield self / "r.JSON"

    def read_launch_request(request_id):
        asked.append(request_id)
        return {"request_id": request_id, "state": "archived"}

    store = SimpleNamespace(launch_requests_dir=Listing(folder), read_launch_request=read_launch_request)

    markers, damaged = cli._lane_launch_markers(store)

    assert asked == ["r"] and markers == [{"request_id": "r", "state": "archived"}] and damaged == "r.JSON"


def test_an_upper_case_suffix_launch_file_is_never_read_as_idle_on_any_volume(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The same rule from the caller's side, whatever the volume does: a `launched` marker file that names the lane and
    ends in `.JSON` is an entry that cannot be read, never "no launch": the acknowledgement of a lane whose checkout
    was moved aside is refused and changes nothing."""
    (tmp_path / "store").mkdir()
    root, base = _repo(tmp_path / "store")
    lane = _assign_worktree(root, base, "upcase")
    store = Store(root)
    store.write_launch_request({"request_id": "lr-up", "state": "launched", "lane_id": "upcase"})
    os.replace(store.launch_requests_dir / "lr-up.json", store.launch_requests_dir / "lr-up.JSON")
    assert "lr-up.JSON" in os.listdir(store.launch_requests_dir)

    reason = cli._lane_worktree_launch_use(store, lane)
    assert reason is not None and "lr-up.JSON" in reason, reason

    moved = tmp_path / "moved" / "elsewhere"
    moved.parent.mkdir()
    assert _git_rc(root, "worktree", "move", lane["worktree_path"], str(moved)).returncode == 0
    before = lanes.load_lanes(store)
    capsys.readouterr()
    assert _run_raw(["lane", "abandon", "--id", "upcase", ACK], root) == 2
    assert "nothing was changed" in capsys.readouterr().err
    assert lanes.load_lanes(store) == before and moved.exists()


def _write_marker_file(store: Store, marker: dict) -> None:
    """A launch-request file with exactly this content (the store refuses to create one in a terminal state, as a
    partly corrupted file is not created through it)."""
    store.launch_requests_dir.mkdir(parents=True, exist_ok=True)
    (store.launch_requests_dir / f"{marker['request_id']}.json").write_text(json.dumps(marker), encoding="utf-8")


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize("state", ["archived", "failed"])
def test_a_terminal_marker_that_the_supervisor_still_lists_as_active_cannot_be_established(
        tmp_path: Path, capsys: pytest.CaptureFixture, state: str, action: str) -> None:
    """GitHub's reviewer on #654 (51fc8ce2): a still-running ephemeral launch's lane-bound marker partly corrupted from
    `launched` to `archived`/`failed` was skipped before its lane was looked at, and the supervisor's active row has no
    path field, so the lane read as idle. A marker that says its launch is over while the supervisor still lists the
    request as active is "could not be established", naming the marker and the record."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "overlive")
    store = Store(root)
    _write_marker_file(store, {"request_id": "lr-over", "state": state, "lane_id": "overlive"})
    _live_ephemeral_launch(store, "lr-over")

    _held_back_or_refused(root, lane, "overlive", action, capsys, "lr-over.json")


@pytest.mark.parametrize("case", ["no-record", "other-request", "other-lane"])
@pytest.mark.parametrize("state", ["archived", "failed"])
def test_a_terminal_marker_the_supervisor_does_not_list_is_skipped_as_before(
        tmp_path: Path, capsys: pytest.CaptureFixture, state: str, case: str) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "finished")
    store = Store(root)
    marker = {"request_id": "lr-done", "state": state, "lane_id": "finished"}
    if case == "other-lane":
        marker["lane_id"] = "elsewhere"                         # a launch of another lane, however it is listed
    _write_marker_file(store, marker)
    if case == "other-request":
        _live_ephemeral_launch(store, "lr-someone-else")
    elif case == "other-lane":
        _live_ephemeral_launch(store, "lr-done")
    wt = Path(lane["worktree_path"])
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "finished"], root) == 0

    record = lanes.load_lanes(store)["lanes"]["finished"]
    assert record["worktree_state"] == lanes.STATUS_ABANDONED and not wt.exists()


def _live_ephemeral_launch(store: Store, request_id: str) -> None:
    """A still-running ephemeral launch: its supervisor record has no folder (`cwd` / `workspace_path`) at all, so only
    its launch-request entry can say that it is there."""
    state = {"agents": {}, "ephemeral_reviewers": {"active": {request_id: {"request_id": request_id}}}}
    (store.dir / "supervisor-state.json").write_text(json.dumps(state), encoding="utf-8")


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize("kind", ["directory", "link-to-directory", "dangling-link"])
def test_a_launch_marker_entry_that_is_not_a_file_is_damage_not_idle(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str, kind: str) -> None:
    """654-R31-1 (the GitHub reviewer): `<request_id>.json` that is a directory (or a link to one) was skipped without
    being noted, so a live ephemeral launch whose record has no folder read as idle and the acknowledgement could
    abandon the live lane. Any `.json` entry that is not a regular file now cannot be established, and is named."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "notafile")
    store = Store(root)
    _live_ephemeral_launch(store, "lr-live")
    entry = store.launch_requests_dir / "lr-live.json"
    store.launch_requests_dir.mkdir(parents=True, exist_ok=True)
    if kind == "directory":
        entry.mkdir()
    else:
        target = tmp_path / "somewhere"
        target.mkdir()
        _link_to(entry, target)
        if kind == "dangling-link":
            target.rmdir()

    _held_back_or_refused(root, lane, "notafile", action, capsys, "lr-live.json")


# The previous client's reading of a lane record, copied from master `ec1104df`: `_lane_branch_gc_allowed` and
# `_lane_worktree_remove_safe` call a lane removable when its `status` OR its `worktree_state` is one of these.
_PREVIOUS_CLIENT_REMOVABLE = {"delivered", "abandoned", "cleanup_failed", "cleanup_pending"}


def _previous_client_calls_removable(lane: dict) -> bool:
    return lane.get("status") in _PREVIOUS_CLIENT_REMOVABLE or lane.get("worktree_state") in _PREVIOUS_CLIENT_REMOVABLE


def test_the_previous_client_reads_an_acknowledged_lane_exactly_like_its_own_cleanup_pending_lane(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """654-R31-2 (the GitHub reviewer asked for a record the previous client refuses to treat as removable): there is
    none that keeps `status` "abandoned". The previous client's two checks (copied above from master) read `status` and
    `worktree_state` as removable when EITHER is one of four words, and its own `cleanup_pending` and `abandoned` are
    both on the list, so a record it wrote itself (abandoned, cleanup pending) is called removable too. The
    acknowledged record is therefore treated by it exactly like its own, which is what master already does; the new
    client keeps its stricter reading. Only a `status` outside the four words would be refused, and it would change
    every reader of the lane's status; that is a decision for the PR, not a hidden encoding."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "prevclient")
    shutil.rmtree(lane["worktree_path"])
    assert _run_raw(["lane", "abandon", "--id", "prevclient", ACK], root) == 0
    store = Store(root)
    acknowledged = lanes.load_lanes(store)["lanes"]["prevclient"]
    pending = {**acknowledged, "worktree_state": lanes.STATUS_CLEANUP_PENDING}      # what the previous client writes
    assert acknowledged["worktree_state"] == lanes.STATUS_CLEANUP_ACKNOWLEDGED

    assert _previous_client_calls_removable(pending) is True
    assert _previous_client_calls_removable(acknowledged) is _previous_client_calls_removable(pending)
    # The new client does keep it: not removable for branch deletion, nor for a worktree that turns up.
    assert cli._lane_branch_gc_allowed(acknowledged)[0] is False
    assert cli._lane_worktree_remove_safe(store, "prevclient", acknowledged, lane["worktree_path"], True)[0] is False
    assert cli._lane_branch_gc_allowed(pending)[0] is True


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize(("state", "inspect"), [
    ({"agents": {"claude-dev": []}}, "supervisor-state.json, agents, record 'claude-dev'"),
    ({"agents": {"claude-dev": "running"}}, "supervisor-state.json, agents, record 'claude-dev'"),
    ({"agents": {}, "ephemeral_reviewers": {"active": []}}, "supervisor-state.json, ephemeral_reviewers"),
    ({"agents": {}, "ephemeral_reviewers": {"active": {"lr-x": []}}},
     "supervisor-state.json, ephemeral_reviewers, record 'lr-x'"),
    ({"agents": {"claude-dev": {"cwd": 5}}}, "record 'claude-dev', field 'cwd' is not text"),
], ids=["agent-record-list", "agent-record-string", "active-is-list", "reviewer-record-list", "cwd-not-text"])
def test_a_damaged_supervisor_record_never_means_idle(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str, state: dict, inspect: str) -> None:
    """PR 654 round 10: the supervisor file only has its outer objects checked when it is loaded; a record inside
    with the wrong shape used to be skipped as idle."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "badrecord")
    (Store(root).dir / "supervisor-state.json").write_text(json.dumps(state), encoding="utf-8")

    _held_back_or_refused(root, lane, "badrecord", action, capsys, inspect)


def test_deliver_says_what_to_inspect_when_a_launch_marker_blocks_the_cleanup(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """PR 654 round 10: `lane deliver` keeps the worktree when it cannot tell whether a launch uses it, and says which
    file to look at instead of only printing "cleanup pending"."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "delivermsg")
    wt = Path(lane["worktree_path"])
    _commit(wt, "src/core/a.py", "base\nmsg\n")
    store = Store(root)
    store.write_launch_request({"request_id": "lr-dm", "state": "queued", "lane_id": "delivermsg"})
    (store.launch_requests_dir / "lr-dm.json").write_text("{not json", encoding="utf-8")
    capsys.readouterr()

    assert _run_raw(["lane", "deliver", "--id", "delivermsg", "--from", "dev"], root) == 0

    captured = capsys.readouterr()
    assert "cleanup pending" in captured.out
    assert "lr-dm.json" in captured.err and "was not removed" in captured.err
    saved = lanes.load_lanes(store)["lanes"]["delivermsg"]
    assert saved["status"] == lanes.STATUS_DELIVERED and wt.exists()


def _gc_json_item(capsys: pytest.CaptureFixture, root: Path, lane_id: str) -> dict:
    capsys.readouterr()
    assert _run_raw(["lane", "gc", "--json"], root) == 0
    return next(i for i in json.loads(capsys.readouterr().out)["items"] if i["lane_id"] == lane_id)


@pytest.mark.parametrize(("command", "times"), [("abandon", 2), ("gc", 1), ("deliver", 1)])
def test_the_help_of_each_command_that_keeps_a_worktree_says_wrap_sessions_are_not_detected(
        capsys: pytest.CaptureFixture, command: str, times: int) -> None:
    """Zoolok's decision on PR 654: a regular `wrap --lane-id` session leaves no record the check can read, so every
    command that relies on the check says so (abandon twice: its description and the acknowledgement option)."""
    with pytest.raises(SystemExit):
        cli.main(["lane", command, "--help"])

    text = " ".join(capsys.readouterr().out.split())
    assert text.count("Running `wrap --lane-id` sessions are not detected; stop them first.") == times


def test_the_acknowledgement_output_says_wrap_sessions_are_not_detected(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "ackline")
    _fail_one_save_then_recover(root, "ackline", monkeypatch)
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "ackline", ACK], root) == 0

    assert "Running `wrap --lane-id` sessions are not detected; stop them first." in capsys.readouterr().out


def _bad_marker(root: Path, lane_id: str, name: str) -> None:
    store = Store(root)
    store.write_launch_request({"request_id": name, "state": "queued", "lane_id": lane_id})
    (store.launch_requests_dir / f"{name}.json").write_text("{not json", encoding="utf-8")


def test_plain_abandon_says_why_the_worktree_was_kept_when_a_marker_cannot_be_read(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """PR 654 round 12: the reason used to be only in the lane record; the command printed just "abandoned lane"."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "keptwhy")
    _bad_marker(root, "keptwhy", "lr-kept")
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "keptwhy"], root) == 0

    captured = capsys.readouterr()
    assert "abandoned lane keptwhy" in captured.out
    for needle in ("keptwhy is abandoned", "could not be established", "lr-kept.json", "was not removed"):
        assert needle in captured.err, needle
    assert Path(lane["worktree_path"]).exists()


@pytest.mark.parametrize("state", ["queued", "launched"])
def test_plain_abandon_says_which_running_launch_kept_the_worktree(
        tmp_path: Path, capsys: pytest.CaptureFixture, state: str) -> None:
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "keptrun")
    Store(root).write_launch_request({"request_id": "lr-run", "state": state, "lane_id": "keptrun"})
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "keptrun"], root) == 0

    err = capsys.readouterr().err
    assert "active or pending launch" in err and "was not removed" in err and "'lr-run'" in err


def test_plain_abandon_prints_no_warning_when_the_worktree_was_removed(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "cleanrun")
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "cleanrun"], root) == 0

    captured = capsys.readouterr()
    assert captured.err == "" and captured.out.strip() == "abandoned lane cleanrun"
    assert not Path(lane["worktree_path"]).exists()


@pytest.mark.parametrize("delete", [False, True])
def test_plain_gc_says_why_a_worktree_was_kept_when_a_marker_cannot_be_read(
        tmp_path: Path, capsys: pytest.CaptureFixture, delete: bool) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "gckept")
    _bad_marker(root, "gckept", "lr-gc")
    assert _run_raw(["lane", "abandon", "--id", "gckept"], root) == 0
    capsys.readouterr()

    assert _run_raw(["lane", "gc", *(["--delete"] if delete else [])], root) == 0

    captured = capsys.readouterr()
    assert "gckept" in captured.out
    for needle in ("gckept", "worktree kept", "could not be established", "lr-gc.json", "was not removed"):
        assert needle in captured.err, needle
    assert Path(lane["worktree_path"]).exists()


def test_plain_gc_says_which_running_launch_kept_the_worktree_and_json_is_unchanged(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "gcrun")
    Store(root).write_launch_request({"request_id": "lr-gcrun", "state": "launched", "lane_id": "gcrun"})
    assert _run_raw(["lane", "abandon", "--id", "gcrun"], root) == 0
    capsys.readouterr()

    assert _run_raw(["lane", "gc"], root) == 0
    err = capsys.readouterr().err
    assert "gcrun" in err and "active or pending launch" in err and "was not removed" in err

    assert _run_raw(["lane", "gc", "--json"], root) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    item = json.loads(captured.out)["items"][0]
    assert item["worktree_remove_safe"] is False and "active or pending launch" in item["worktree_reason"]


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize(("primary", "inspect"), [
    ("{not json", "supervisor-state.json file in the store cannot be read; its backup copy is not used"),
    (None, "supervisor-state.json file in the store is missing and only its backup copy is left"),
], ids=["primary-corrupt", "primary-missing"])
def test_an_older_backup_of_the_supervisor_file_never_stands_in_for_the_real_one(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str, primary: str | None, inspect: str) -> None:
    """PR 654 round 13: when `supervisor-state.json` cannot be read, the loader quietly returns its older `.bak`
    copy, which may predate the launch that uses the lane. The check reads the real file strictly instead."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "bakonly")
    state_file = Store(root).dir / "supervisor-state.json"
    Path(f"{state_file}.bak").write_text(json.dumps({"agents": {}}), encoding="utf-8")   # valid, shows no launch
    if primary is not None:
        state_file.write_text(primary, encoding="utf-8")

    _held_back_or_refused(root, lane, "bakonly", action, capsys, inspect)


def test_a_readable_supervisor_file_is_used_even_when_its_backup_is_damaged(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "badbak")
    state_file = Store(root).dir / "supervisor-state.json"
    state_file.write_text(json.dumps({"agents": {}}), encoding="utf-8")
    Path(f"{state_file}.bak").write_text("{not json", encoding="utf-8")
    shutil.rmtree(lane["worktree_path"])
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "badbak", ACK], root) == 0

    assert lanes.load_lanes(Store(root))["lanes"]["badbak"]["worktree_state"] == lanes.STATUS_CLEANUP_ACKNOWLEDGED


def _idle_backup_beside_a_busy_file_that_is_then_damaged(root: Path, wt: Path) -> None:
    """What the supervisor really leaves behind: two saves (the second keeps the first as `.bak`), then only the
    current file is damaged. The backup is idle, the current file said a session was working in the lane's folder."""
    from agenttalk import supervisor as sup

    path = Store(root).dir / "supervisor-state.json"
    sup.save_supervisor_state(path, {"agents": {}})
    sup.save_supervisor_state(path, {"agents": {"claude-dev": {"cwd": str(wt)}}})
    assert json.loads(Path(f"{path}.bak").read_text(encoding="utf-8")) == {"agents": {}}
    path.write_text("{not json", encoding="utf-8")


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_a_real_backup_generation_never_hides_a_busy_supervisor_file_from_abandon_and_the_acknowledgement(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "realbak")
    _idle_backup_beside_a_busy_file_that_is_then_damaged(root, Path(lane["worktree_path"]))

    _held_back_or_refused(
        root, lane, "realbak", action, capsys, "supervisor-state.json file in the store cannot be read")


def test_a_real_backup_generation_never_hides_a_busy_supervisor_file_from_deliver(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "realbakdel")
    wt = Path(lane["worktree_path"])
    _commit(wt, "src/core/a.py", "base\nbak\n")
    _idle_backup_beside_a_busy_file_that_is_then_damaged(root, wt)
    capsys.readouterr()

    assert _run_raw(["lane", "deliver", "--id", "realbakdel", "--from", "dev"], root) == 0

    captured = capsys.readouterr()
    assert "cleanup pending" in captured.out
    assert "supervisor-state.json" in captured.err and "was not removed" in captured.err
    assert wt.exists()


def test_a_real_backup_generation_never_hides_a_busy_supervisor_file_from_gc(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "realbakgc")
    wt = Path(lane["worktree_path"])
    _idle_backup_beside_a_busy_file_that_is_then_damaged(root, wt)
    assert _run_raw(["lane", "abandon", "--id", "realbakgc"], root) == 0
    capsys.readouterr()

    item = _gc_json_item(capsys, root, "realbakgc")

    assert item["worktree_remove_safe"] is False
    assert "could not be established" in item["worktree_reason"] and "supervisor-state.json" in item["worktree_reason"]
    assert wt.exists()


def _write_marker(root: Path, rid: str, **fields: object) -> None:
    store = Store(root)
    store.write_launch_request({"request_id": rid, "state": "queued"})       # makes the folder
    (store.launch_requests_dir / f"{rid}.json").write_text(
        json.dumps({"request_id": rid, "state": "queued", **fields}), encoding="utf-8")


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize("fields", [
    {"lane_id": 5},
    {"lane_id": ""},
    {"scope": "oops"},
    {"scope": None},
    {"scope": {"lane_id": 5}},
    {"scope": {"lane_id": ""}},
    {"lane_id": "other", "scope": {"lane_id": "another"}},
], ids=["lane-id-number", "lane-id-empty", "scope-text", "scope-null", "scope-lane-number", "scope-lane-empty",
        "two-bindings-disagree"])
def test_a_launch_marker_with_a_damaged_lane_binding_never_means_idle(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str, fields: dict) -> None:
    """PR 654 round 15: the marker is still valid JSON, so the store returns it, but it cannot be told apart from a
    launch for this lane; it used to be skipped as unrelated."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "badbind")
    _write_marker(root, "lr-dmg", **fields)

    _held_back_or_refused(root, lane, "badbind", action, capsys, "lr-dmg.json")


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize("fields", [
    {"lane_id": "other"},
    {"lane_id": "other", "scope": {"lane_id": "other", "revision": "a" * 40, "paths": []}},
    {"scope": {"revision": "a" * 40, "paths": []}},
    {"lane_id": None, "scope": {"revision": "a" * 40, "lane_id": None}},
    {},
    {"state": "failed", "lane_id": 5},
], ids=["other-lane", "other-lane-both", "lane-less", "null-bindings", "no-binding", "finished-damaged"])
def test_a_launch_marker_that_is_unrelated_to_the_lane_lets_it_go(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str, fields: dict) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "unrelated")
    wt = Path(lane["worktree_path"])
    _write_marker(root, "lr-other", **fields)
    capsys.readouterr()

    if action == "abandon":
        assert _run_raw(["lane", "abandon", "--id", "unrelated"], root) == 0
        assert not wt.exists()
        assert lanes.load_lanes(Store(root))["lanes"]["unrelated"]["worktree_state"] == lanes.STATUS_ABANDONED
    else:
        shutil.rmtree(wt)
        assert _run_raw(["lane", "abandon", "--id", "unrelated", ACK], root) == 0
        record = lanes.load_lanes(Store(root))["lanes"]["unrelated"]
        assert record["worktree_state"] == lanes.STATUS_CLEANUP_ACKNOWLEDGED


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize("fields", [
    {"lane_id": "other", "scope": {"lane_id": 5}},
    {"lane_id": "other", "scope": {"lane_id": ""}},
    {"lane_id": "other", "scope": "oops"},
    {"lane_id": "other", "scope": None},
    {"lane_id": 5, "scope": {"lane_id": "other"}},
    {"lane_id": "", "scope": {"lane_id": "other"}},
], ids=["other+scope-lane-number", "other+scope-lane-empty", "other+scope-text", "other+scope-null",
        "lane-number+scope-other", "lane-empty+scope-other"])
def test_a_valid_binding_for_another_lane_never_hides_a_damaged_one_in_the_other_place(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str, fields: dict) -> None:
    """PR 654 round 16 (Zoolok): both places are checked, the direct `lane_id` and the one inside `scope`."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "bothplaces")
    _write_marker(root, "lr-both", **fields)

    _held_back_or_refused(root, lane, "bothplaces", action, capsys, "lr-both.json")


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize("fields", [
    {"lane_id": "thislane", "scope": {"lane_id": 5}},
    {"lane_id": "thislane", "scope": "oops"},
    {"lane_id": 5, "scope": {"lane_id": "thislane"}},
    {"lane_id": "other", "scope": {"lane_id": "thislane"}},
], ids=["this+scope-number", "this+scope-text", "number+scope-this", "other+scope-this"])
def test_a_field_that_names_this_lane_makes_it_in_use_even_if_the_other_place_is_damaged(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str, fields: dict) -> None:
    """A field that names this lane is a found launch: the lane is in use (not "could not be established"), and that
    also holds back cleanup."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "thislane")
    wt = Path(lane["worktree_path"])
    _write_marker(root, "lr-this", **fields)
    capsys.readouterr()

    if action == "abandon":
        assert _run_raw(["lane", "abandon", "--id", "thislane"], root) == 0
        record = lanes.load_lanes(Store(root))["lanes"]["thislane"]
        assert record["worktree_state"] == lanes.STATUS_CLEANUP_PENDING and wt.exists()
        assert "active or pending launch" in record["worktree_cleanup_error"]
        assert "'lr-this'" in record["worktree_cleanup_error"]
    else:
        shutil.rmtree(wt)
        before = lanes.load_lanes(Store(root))
        capsys.readouterr()
        assert _run_raw(["lane", "abandon", "--id", "thislane", ACK], root) == 2
        err = capsys.readouterr().err
        assert "active or pending launch" in err and "'lr-this'" in err
        assert lanes.load_lanes(Store(root)) == before


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize("state", [
    {"agents": None},
    {"ephemeral_reviewers": None},
    {"agents": None, "ephemeral_reviewers": None},
    {"agents": {}, "ephemeral_reviewers": {"active": None}},
    {"agents": {}, "ephemeral_reviewers": {}},
    {"agents": {"claude-dev": {}}},
    {"agents": {"claude-dev": {"cwd": ""}}},
    {"agents": {"claude-dev": {"cwd": None, "launch_cwd": "", "workspace_path": None}}},
    {"agents": {}, "ephemeral_reviewers": {"active": {"lr-x": {"phase": "launched"}}}},
], ids=["agents-null", "reviewers-null", "both-null", "active-null", "active-missing", "record-no-fields",
        "cwd-empty", "all-fields-null-or-empty", "reviewer-record-no-folder"])
def test_null_sections_and_empty_folder_fields_in_the_supervisor_file_stay_idle(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str, state: dict) -> None:
    """PR 654 round 16 (Zoolok): the supervisor's own writer and loader accept these shapes and older records omit
    them, so they are not "damaged"; a lane with only such records goes as usual."""
    from agenttalk import supervisor as sup

    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "nullsec")
    wt = Path(lane["worktree_path"])
    path = Store(root).dir / "supervisor-state.json"
    sup.save_supervisor_state(path, state)                            # the supervisor's own validator
    assert sup.load_supervisor_state(path) == state                   # and its own loader
    capsys.readouterr()

    if action == "abandon":
        assert _run_raw(["lane", "abandon", "--id", "nullsec"], root) == 0
        assert not wt.exists()
        assert lanes.load_lanes(Store(root))["lanes"]["nullsec"]["worktree_state"] == lanes.STATUS_ABANDONED
    else:
        shutil.rmtree(wt)
        assert _run_raw(["lane", "abandon", "--id", "nullsec", ACK], root) == 0
        record = lanes.load_lanes(Store(root))["lanes"]["nullsec"]
        assert record["worktree_state"] == lanes.STATUS_CLEANUP_ACKNOWLEDGED


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize("fields", [
    {"lane_id": "bad id", "scope": {"lane_id": "bad id"}},
    {"lane_id": "bad id"},
    {"scope": {"lane_id": "bad id"}},
    {"lane_id": "a/b"},
    {"lane_id": "x.lock"},
    {"lane_id": "-x"},
    {"lane_id": "bad id", "scope": {"lane_id": "other"}},
    {"lane_id": "other", "scope": {"lane_id": "bad id"}},
    {"workspace_path": 5},
], ids=["both-same-invalid", "direct-invalid", "scope-invalid", "slash", "lock-suffix", "leading-dash",
        "invalid+other", "other+invalid", "workspace-path-number"])
def test_a_lane_id_that_is_not_a_valid_lane_id_in_a_marker_is_damage(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str, fields: dict) -> None:
    """PR 654 round 17: two equal but invalid ids (`bad id`) used to pass both the damage check and the equality
    check and read as an unrelated launch."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "syntax")
    _write_marker(root, "lr-syn", **fields)

    _held_back_or_refused(root, lane, "syntax", action, capsys, "lr-syn.json")


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize("fields", [
    {},
    {"lane_id": 5},
    {"lane_id": "bad id", "scope": {"lane_id": "bad id"}},
    {"lane_id": "other"},
], ids=["lane-less", "lane-id-number", "both-invalid", "other-lane"])
def test_a_marker_whose_workspace_path_names_this_worktree_is_in_use_even_if_its_lane_fields_are_unreadable(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str, fields: dict) -> None:
    """The supervisor puts the lane's folder into a lane launch's marker as `workspace_path`; a marker naming this
    folder is a launch using it, whatever its lane fields look like."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "wspath")
    wt = Path(lane["worktree_path"])
    _write_marker(root, "lr-ws", workspace_path=str(wt), **fields)
    capsys.readouterr()

    if action == "abandon":
        assert _run_raw(["lane", "abandon", "--id", "wspath"], root) == 0
        record = lanes.load_lanes(Store(root))["lanes"]["wspath"]
        assert record["worktree_state"] == lanes.STATUS_CLEANUP_PENDING and wt.exists()
        assert "active or pending launch" in record["worktree_cleanup_error"]
        assert "'lr-ws'" in record["worktree_cleanup_error"]
    else:
        shutil.rmtree(wt)
        before = lanes.load_lanes(Store(root))
        capsys.readouterr()
        assert _run_raw(["lane", "abandon", "--id", "wspath", ACK], root) == 2
        err = capsys.readouterr().err
        assert "active or pending launch" in err and "'lr-ws'" in err
        assert lanes.load_lanes(Store(root)) == before


@pytest.mark.parametrize("workspace", ["", "elsewhere"], ids=["empty", "another-folder"])
def test_a_marker_whose_workspace_path_is_empty_or_names_another_folder_is_unrelated(
        tmp_path: Path, capsys: pytest.CaptureFixture, workspace: str) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "wsother")
    wt = Path(lane["worktree_path"])
    _write_marker(root, "lr-wo", workspace_path=str(tmp_path / workspace) if workspace else "")
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "wsother"], root) == 0

    assert not wt.exists()


def _cannot_resolve(monkeypatch: pytest.MonkeyPatch, *paths: str) -> None:
    """Make the real `canonical_host_path` fail with an OS error for exactly these inputs (and only these)."""
    real = lanes.canonical_host_path

    def canonical(value):  # noqa: ANN001, ANN202
        if os.fspath(value) in paths:
            raise OSError("injected: this path cannot be resolved")
        return real(value)

    real_checked = lanes.canonical_host_path_checked

    def canonical_checked(value):  # noqa: ANN001, ANN202
        if os.fspath(value) in paths:
            raise OSError("injected: this path cannot be resolved")
        return real_checked(value)

    monkeypatch.setattr(lanes, "canonical_host_path", canonical)
    monkeypatch.setattr(lanes, "canonical_host_path_checked", canonical_checked)


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_a_marker_folder_that_cannot_be_resolved_is_unknown_and_never_unrelated(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, action: str) -> None:
    """PR 654 round 18: the marker's only binding is a `workspace_path` that does name this worktree, but resolving
    it fails. That used to read as "a different folder", so plain abandon removed the checkout."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "pathfail")
    store = Store(root)
    alias = str(Path(lane["worktree_path"])) + os.sep + "."
    store.write_launch_request({"request_id": "lr-path-error", "state": "launched", "scope": {},
                                "workspace_path": alias})
    assert cli._lane_worktree_launch_use(store, lane).startswith(cli._LANE_WORKTREE_BUSY)   # the alias does match
    _cannot_resolve(monkeypatch, alias)

    _held_back_or_refused(root, lane, "pathfail", action, capsys, "lr-path-error.json")


def test_a_definite_match_still_wins_over_a_marker_folder_that_cannot_be_resolved(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "pathwin")
    store = Store(root)
    bad = "/nonexistent-folder-for-this-test/x"
    store.write_launch_request({"request_id": "lr-a-bad", "state": "launched", "scope": {}, "workspace_path": bad})
    store.write_launch_request({"request_id": "lr-z-ok", "state": "launched", "scope": {},
                                "workspace_path": str(Path(lane["worktree_path"])) + os.sep + "."})
    _cannot_resolve(monkeypatch, bad)

    reason = cli._lane_worktree_launch_use(store, lane)

    assert reason is not None and reason.startswith(cli._LANE_WORKTREE_BUSY) and "'lr-z-ok'" in reason


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_a_lane_whose_own_worktree_path_cannot_be_resolved_is_unknown(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, action: str) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "ownfail")
    _cannot_resolve(monkeypatch, lane["worktree_path"])

    _held_back_or_refused(root, lane, "ownfail", action, capsys, "worktree path cannot be resolved")


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_a_supervisor_record_folder_that_cannot_be_resolved_is_unknown(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, action: str) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "recfail")
    odd = "/nonexistent-folder-for-this-test/y"
    state = {"agents": {"claude-dev": {"cwd": odd}}}
    (Store(root).dir / "supervisor-state.json").write_text(json.dumps(state), encoding="utf-8")
    _cannot_resolve(monkeypatch, odd)

    _held_back_or_refused(
        root, lane, "recfail", action, capsys, "supervisor-state.json, agents, record 'claude-dev', field 'cwd' cannot")


def _stat_fails(monkeypatch: pytest.MonkeyPatch, *paths: Path, error: OSError | None = None) -> None:
    """Looking at exactly these paths fails with an error that is not "not found". `os.stat` raises, and, as recent
    Pythons do, `os.path.exists` (which `Path.exists()` uses) swallows it and answers False."""
    bad = {os.path.normcase(os.fspath(p)) for p in paths}
    real_stat, real_exists = os.stat, os.path.exists

    def is_bad(path: object) -> bool:
        return isinstance(path, (str, bytes, os.PathLike)) and os.path.normcase(os.fsdecode(path)) in bad

    def stat(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if is_bad(path):
            raise error or PermissionError(13, "injected: access denied")
        return real_stat(path, *args, **kwargs)

    def exists(path):  # noqa: ANN001, ANN202
        return False if is_bad(path) else real_exists(path)

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os.path, "exists", exists)


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_a_launch_requests_folder_that_cannot_be_looked_at_is_unknown_not_absent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, action: str) -> None:
    """PR 654 round 19: `Path.exists()` answers False for any OS error, so an unreadable folder with an active marker
    in it read as "no markers"."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "statdir")
    store = Store(root)
    store.write_launch_request({"request_id": "lr-statdir", "state": "launched", "lane_id": "statdir"})
    _stat_fails(monkeypatch, store.launch_requests_dir)

    _held_back_or_refused(
        root, lane, "statdir", action, capsys, "launch-requests folder cannot be checked")


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize("which", ["state", "backup"])
def test_a_supervisor_file_that_cannot_be_looked_at_is_unknown_not_absent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, action: str,
        which: str) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "statfile")
    state_file = Store(root).dir / "supervisor-state.json"
    busy = {"agents": {"claude-dev": {"cwd": lane["worktree_path"]}}}
    if which == "state":
        state_file.write_text(json.dumps(busy), encoding="utf-8")
        _stat_fails(monkeypatch, state_file)
    else:
        _stat_fails(monkeypatch, Path(f"{state_file}.bak"))               # no state file at all; the backup is odd

    _held_back_or_refused(
        root, lane, "statfile", action, capsys, "supervisor-state.json file in the store (or its backup) cannot be")


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_a_launch_requests_folder_that_is_a_dangling_link_is_unknown_not_absent(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "dangling")
    folder = Store(root).launch_requests_dir
    assert not os.path.lexists(folder)
    target = tmp_path / "target-that-goes-away"
    target.mkdir()
    try:
        if os.name == "nt":                                                 # a junction needs no special privilege
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(folder), str(target)],
                                  capture_output=True, text=True)
            if made.returncode != 0:
                pytest.skip(f"cannot create a junction here: {(made.stdout + made.stderr).strip()[:120]}")
        else:
            os.symlink(target, folder, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"cannot create a link here: {exc}")
    target.rmdir()                                                          # the link now points at nothing
    assert os.path.lexists(folder) and not os.path.exists(folder)

    _held_back_or_refused(
        root, lane, "dangling", action, capsys, "launch-requests folder cannot be checked")


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_a_stat_error_on_one_marker_file_is_named_for_that_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, action: str) -> None:
    """PR 654 round 20: the file cannot be read, and then looking at it fails with an I/O error too. The error used to
    escape the whole walk, and the message did not say which file."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "onefile")
    store = Store(root)
    store.write_launch_request({"request_id": "lr-unreadable-stat", "state": "queued", "lane_id": "other"})
    _stat_fails(monkeypatch, store.launch_requests_dir / "lr-unreadable-stat.json",
                error=OSError(errno.EIO, "injected: input/output error"))

    _held_back_or_refused(root, lane, "onefile", action, capsys, "'lr-unreadable-stat.json'")


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_a_later_marker_that_names_this_lane_still_wins_over_a_file_that_cannot_be_looked_at(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, action: str) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "walkon")
    wt = Path(lane["worktree_path"])
    store = Store(root)
    store.write_launch_request({"request_id": "lr-a-unreadable", "state": "queued", "lane_id": "other"})
    store.write_launch_request({"request_id": "lr-z-this", "state": "launched", "lane_id": "walkon"})
    _stat_fails(monkeypatch, store.launch_requests_dir / "lr-a-unreadable.json",
                error=OSError(errno.EIO, "injected: input/output error"))
    capsys.readouterr()

    if action == "abandon":
        assert _run_raw(["lane", "abandon", "--id", "walkon"], root) == 0
        error = lanes.load_lanes(store)["lanes"]["walkon"]["worktree_cleanup_error"]
        assert "active or pending launch" in error and "'lr-z-this'" in error and wt.exists()
    else:
        shutil.rmtree(wt)
        before = lanes.load_lanes(store)
        capsys.readouterr()
        assert _run_raw(["lane", "abandon", "--id", "walkon", ACK], root) == 2
        err = capsys.readouterr().err
        assert "active or pending launch" in err and "'lr-z-this'" in err
        assert lanes.load_lanes(store) == before


def _hidden_error(monkeypatch: pytest.MonkeyPatch, *paths: Path, swallow: tuple[str, ...] = (),
                  lstat: bool = False, only_during: str | None = None) -> list[str]:
    """Looking at exactly these paths fails (`os.stat`, and `os.lstat` when asked, raise PermissionError). Each
    `os.path` function named in `swallow` ("exists", "isdir", "lexists") answers False for them instead, as Python
    3.14's `Path` methods do with any OS error. With `only_during`, the fault is armed only while that `cli` function
    runs. Returns the list of calls the fault fired on, so a test can assert that it did."""
    bad = {os.path.normcase(os.fspath(p)) for p in paths}
    fired: list[str] = []
    armed = {"on": only_during is None}

    def is_bad(path: object) -> bool:
        return (armed["on"] and isinstance(path, (str, bytes, os.PathLike))
                and os.path.normcase(os.fsdecode(path)) in bad)

    def wrap_raising(name: str, real):  # noqa: ANN001, ANN202
        def look(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            if is_bad(path):
                fired.append(name)
                raise PermissionError(13, "injected: access denied")
            return real(path, *args, **kwargs)
        return look

    def wrap_swallowing(name: str, real):  # noqa: ANN001, ANN202
        def look(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            if is_bad(path):
                fired.append(name)
                return False
            return real(path, *args, **kwargs)
        return look

    monkeypatch.setattr(os, "stat", wrap_raising("stat", os.stat))
    if lstat:
        monkeypatch.setattr(os, "lstat", wrap_raising("lstat", os.lstat))
    for name in swallow:
        monkeypatch.setattr(os.path, name, wrap_swallowing(name, getattr(os.path, name)))
    if only_during is not None:
        real_fn = getattr(cli, only_during)

        def armed_fn(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            armed["on"] = True
            try:
                return real_fn(*args, **kwargs)
            finally:
                armed["on"] = False

        monkeypatch.setattr(cli, only_during, armed_fn)
    return fired


@pytest.mark.parametrize("action", ["abandon", "acknowledge", "gc"])
def test_a_marker_file_whose_directory_check_fails_is_unknown_for_every_caller(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, action: str) -> None:
    """PR 654 round 21 (1): `Path.is_dir()` swallows stat errors; the marker walk now asks with a stat that raises. The
    fault is only at that check (the later read of the file would have worked), so the old walk read the marker as
    an unrelated launch."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "isdirfail")
    store = Store(root)
    store.write_launch_request({"request_id": "lr-isdir", "state": "launched", "lane_id": "other"})
    fired = _hidden_error(monkeypatch, store.launch_requests_dir / "lr-isdir.json", swallow=("isdir",))

    if action == "gc":
        assert _run_raw(["lane", "abandon", "--id", "isdirfail"], root) == 0         # held back: pending
        capsys.readouterr()
        assert _run_raw(["lane", "gc", "--json"], root) == 0
        item = json.loads(capsys.readouterr().out)["items"][0]
        assert item["worktree_remove_safe"] is False and "'lr-isdir.json'" in item["worktree_reason"]
        assert Path(lane["worktree_path"]).exists()
    else:
        _held_back_or_refused(root, lane, "isdirfail", action, capsys, "'lr-isdir.json'")
    assert fired


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_a_marker_the_store_cannot_read_because_its_own_check_fails_is_unknown(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, action: str) -> None:
    """Round 21 (2): `Store.read_launch_request` does `p.exists()` and turns a failure into None. The check's own
    handling of that None looks at the file with a strict stat, finds it there, and names it."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "storefail")
    store = Store(root)
    store.write_launch_request({"request_id": "lr-storefail", "state": "launched", "lane_id": "other"})
    target = store.launch_requests_dir / "lr-storefail.json"
    real_exists = Path.exists
    fired: list[Path] = []

    def exists(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if self == target:
            fired.append(self)
            return False
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists)

    _held_back_or_refused(root, lane, "storefail", action, capsys, "'lr-storefail.json'")
    assert fired


def test_delivery_does_not_record_the_cleanup_as_done_when_the_worktree_cannot_be_looked_at(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """Round 21 (3): `wt.exists()` before the in-use check answered False for a hidden error, so delivery recorded
    `cleanup_pending=False` while the checkout was still there."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "delfail")
    wt = Path(lane["worktree_path"])
    _commit(wt, "src/core/a.py", "base\nfail\n")
    fired = _hidden_error(monkeypatch, wt, swallow=("exists", "lexists"), lstat=True,
                          only_during="_lane_finalize_delivery")
    capsys.readouterr()

    assert _run_raw(["lane", "deliver", "--id", "delfail", "--from", "dev"], root) == 0

    captured = capsys.readouterr()
    saved = lanes.load_lanes(Store(root))["lanes"]["delfail"]
    assert saved["status"] == lanes.STATUS_DELIVERED and saved.get("cleanup_pending") is True
    assert "cleanup pending" in captured.out and "could not be established" in captured.err
    assert wt.exists() and fired


def test_the_acknowledgement_does_not_accept_a_folder_it_cannot_look_at(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """Round 21 (4): `os.path.lexists(wt)` answered False for a hidden lstat error, so the acknowledgement was
    recorded with the checkout still there."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "ackfail")
    wt = Path(lane["worktree_path"])
    store = Store(root)
    before = lanes.load_lanes(store)
    fired = _hidden_error(monkeypatch, wt, swallow=("exists", "lexists"), lstat=True)
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "ackfail", ACK], root) == 2

    err = capsys.readouterr().err
    monkeypatch.undo()                                                  # look at the folder with the fault gone
    assert "could not be established" in err and "nothing was changed" in err
    assert lanes.load_lanes(store) == before and wt.exists() and fired


def _realpath_hides_errors(monkeypatch: pytest.MonkeyPatch, *paths: str) -> list[str]:
    """What Windows' `ntpath.realpath` does without `strict=True`: for these paths an access-denied error is absorbed
    and the path comes back as given; with `strict=True` it is raised."""
    real = os.path.realpath
    fired: list[str] = []

    def realpath(path, *args, strict=False, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if os.fspath(path) in paths:
            if strict:
                fired.append(os.fspath(path))
                raise PermissionError(13, "injected: access denied")
            return os.fspath(path)
        return real(path, *args, strict=strict, **kwargs)

    monkeypatch.setattr(os.path, "realpath", realpath)
    return fired


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize("where", ["marker-folder", "supervisor-cwd", "lane-own-path"])
def test_a_path_that_realpath_cannot_resolve_is_unknown_not_a_different_folder(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, action: str,
        where: str) -> None:
    """Round 21 (5): without `strict=True`, `realpath` absorbs some errors and returns a partial path that cannot
    equal the worktree, so the old comparison read "a different folder"."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "rpfail")
    store = Store(root)
    alias = str(Path(lane["worktree_path"])) + os.sep + "."
    if where == "marker-folder":
        store.write_launch_request({"request_id": "lr-rp", "state": "launched", "scope": {}, "workspace_path": alias})
        inspect, bad = "'lr-rp.json'", alias
    elif where == "supervisor-cwd":
        state = {"agents": {"claude-dev": {"cwd": alias}}}
        (store.dir / "supervisor-state.json").write_text(json.dumps(state), encoding="utf-8")
        inspect, bad = "record 'claude-dev', field 'cwd' cannot be resolved", alias
    else:
        inspect, bad = "worktree path cannot be resolved", lane["worktree_path"]
    if where != "lane-own-path":
        assert cli._lane_worktree_launch_use(store, lane).startswith(cli._LANE_WORKTREE_BUSY)   # the alias matches
    fired = _realpath_hides_errors(monkeypatch, bad)

    _held_back_or_refused(root, lane, "rpfail", action, capsys, inspect)
    assert fired


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_a_marker_whose_state_is_not_text_names_the_marker_file(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str) -> None:
    """Round 21 (6): a list-valued state used to raise TypeError into a handler that blamed the folder."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "listState")
    _write_marker(root, "lr-liststate", state=["queued"], lane_id="other")

    _held_back_or_refused(root, lane, "listState", action, capsys, "'lr-liststate.json'")


def _second_lookup_is_tolerant(monkeypatch: pytest.MonkeyPatch, alias: str, partial: str) -> dict[str, int]:
    """The first, strict lookup of `alias` works. A second, tolerant lookup (what the old resolver made after its
    strict one) absorbs an error and hands back `partial`, as `ntpath.realpath` does on Windows. Portable: it is a
    wrapper around `os.path.realpath`, the one function every resolution goes through."""
    real = os.path.realpath
    calls = {"strict": 0, "tolerant": 0}

    def realpath(path, *args, strict=False, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if os.fspath(path) == alias:
            if strict:
                calls["strict"] += 1
                return real(path, *args, strict=True, **kwargs)
            calls["tolerant"] += 1
            return partial
        return real(path, *args, strict=strict, **kwargs)

    monkeypatch.setattr(os.path, "realpath", realpath)
    return calls


@pytest.mark.parametrize("caller", ["abandon", "acknowledge", "gc-delete", "deliver"])
def test_the_checked_resolver_never_resolves_again_tolerantly(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, caller: str) -> None:
    """PR 654 round 22: after its strict lookup the resolver used to call the tolerant `canonical_host_path`, which
    looked the path up a second time and, when that lookup absorbed an error, returned a partial path: the marker's
    folder then read as "a different folder", the check as idle, and the checkout was removed (or the
    acknowledgement accepted)."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "twice1")
    wt = Path(lane["worktree_path"])
    store = Store(root)
    alias = str(wt) + os.sep + "."
    store.write_launch_request({"request_id": "lr-twice", "state": "launched", "scope": {}, "workspace_path": alias})
    assert cli._lane_worktree_launch_use(store, lane).startswith(cli._LANE_WORKTREE_BUSY)    # the alias matches
    calls = _second_lookup_is_tolerant(monkeypatch, alias, str(wt.parent))
    capsys.readouterr()

    if caller == "abandon":
        assert _run_raw(["lane", "abandon", "--id", "twice1"], root) == 0
        record = lanes.load_lanes(store)["lanes"]["twice1"]
        assert record["worktree_state"] == lanes.STATUS_CLEANUP_PENDING and wt.exists()
        assert "'lr-twice'" in record["worktree_cleanup_error"]
    elif caller == "acknowledge":
        shutil.rmtree(wt)
        before = lanes.load_lanes(store)
        capsys.readouterr()
        assert _run_raw(["lane", "abandon", "--id", "twice1", ACK], root) == 2
        assert "'lr-twice'" in capsys.readouterr().err and lanes.load_lanes(store) == before
    elif caller == "gc-delete":
        assert _run_raw(["lane", "abandon", "--id", "twice1"], root) == 0
        assert wt.exists()
        capsys.readouterr()
        assert _run_raw(["lane", "gc", "--delete", "--json"], root) == 0
        item = json.loads(capsys.readouterr().out)["items"][0]
        assert item["worktree_remove_safe"] is False and "worktree_removed" not in item and wt.exists()
    else:
        _commit(wt, "src/core/a.py", "base\ntwice\n")
        assert _run_raw(["lane", "deliver", "--id", "twice1", "--from", "dev"], root) == 0
        captured = capsys.readouterr()
        assert "cleanup pending" in captured.out and "'lr-twice'" in captured.err and wt.exists()
        assert lanes.load_lanes(store)["lanes"]["twice1"].get("cleanup_pending") is True
    assert calls["strict"] >= 1 and calls["tolerant"] == 0


def test_a_path_that_is_truly_absent_resolves_under_a_strict_link_free_parent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 26: the only absent path the checked resolver accepts is a missing last name under a parent that resolves
    strictly. For those it gives what the plain resolver gives."""
    (tmp_path / "exists").mkdir()
    monkeypatch.chdir(tmp_path)
    cases = [
        str(tmp_path / "exists"),
        str(tmp_path / "exists") + os.sep + ".",
        str(tmp_path / "exists" / "missing"),
        str(tmp_path / "exists" / "missing") + os.sep,
        "exists",
        os.path.join("exists", "missing"),
    ]
    for case in cases:
        assert lanes.canonical_host_path_checked(case) == lanes.canonical_host_path(case), case


def test_a_path_that_cannot_be_proven_absent_is_refused(tmp_path: Path) -> None:
    """A missing parent, a `..` in the path, and a parent that is a file are not provable (a false refusal is
    acceptable, a false idle is not)."""
    (tmp_path / "exists").mkdir()
    (tmp_path / "file").write_text("x", encoding="utf-8")
    cases = [
        tmp_path / "missing" / "deeper" / "still",
        tmp_path / "exists" / ".." / "exists" / "missing",
        tmp_path / "file" / "x",
    ]
    for case in cases:
        with pytest.raises(OSError):
            lanes.canonical_host_path_checked(case)


def _link_to(link: Path, target: Path) -> None:
    """A directory link at `link` pointing at `target`: a junction on Windows (no special privilege), else a symlink."""
    try:
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                  capture_output=True, text=True)
            if made.returncode != 0:
                pytest.skip(f"cannot create a junction here: {(made.stdout + made.stderr).strip()[:120]}")
        else:
            os.symlink(target, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"cannot create a link here: {exc}")


def test_a_link_or_junction_anywhere_on_the_way_makes_an_absent_path_unprovable(tmp_path: Path) -> None:
    """A dangling link or junction (whose target is missing), a link in front of a missing name, and a link to a link
    are all refused: no emulation of how links are followed."""
    real, gone = tmp_path / "real", tmp_path / "gone"
    real.mkdir()
    gone.mkdir()
    live, dangling, chained = tmp_path / "live", tmp_path / "dangling", tmp_path / "chained"
    _link_to(live, real)
    _link_to(dangling, gone)
    _link_to(chained, dangling)
    gone.rmdir()                                                         # `dangling` and `chained` now point at nothing
    sep = os.sep
    cases: list[str | Path] = [
        live / "missing",                    # an existing link in front of a missing name
        dangling, dangling / "child", dangling / "a" / "b", chained, chained / "x",
    ]
    # Raw strings with a trailing separator or "." part: not wrapped in Path, which would erase the suffix.
    for link in (str(dangling), str(chained)):
        cases += [link + sep, link + sep + ".", link + sep + sep, link + sep + "." + sep, link + "/", link + "/."]
    for case in cases:
        with pytest.raises(OSError):
            lanes.canonical_host_path_checked(case)
    assert lanes.canonical_host_path_checked(live) == lanes.canonical_host_path(real)   # a live link still resolves
    for suffix in (sep, sep + ".", sep + sep, sep + "." + sep):
        assert lanes.canonical_host_path_checked(str(live) + suffix) == lanes.canonical_host_path(real)


@pytest.mark.parametrize("suffix", ["/", "/.", "//", "/./"],
                         ids=["slash", "slash-dot", "double-slash", "slash-dot-slash"])
def test_a_raw_suffix_after_a_dangling_link_is_refused_even_when_lstat_follows_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str) -> None:
    """The absence check looks at the exact entry that would be returned (the link itself), not at the raw string."""
    target, link, live = tmp_path / "target", tmp_path / "link", tmp_path / "live"
    target.mkdir()
    _link_to(link, target)
    _link_to(live, tmp_path)
    target.rmdir()
    _lstat_follows_a_trailing_suffix(monkeypatch)

    with pytest.raises(OSError):
        lanes.canonical_host_path_checked(str(link) + suffix)
    assert lanes.canonical_host_path_checked(str(live) + suffix) == lanes.canonical_host_path(live)


def test_a_denied_lookup_of_the_parent_or_the_entry_is_raised_not_swallowed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The strict lookup of the parent, `lstat` of the path itself, and `lstat` of a component on the way each raise
    when they fail for any reason other than "not found"."""
    (tmp_path / "a").mkdir()
    real_realpath, real_lstat = os.path.realpath, os.lstat
    fired: list[str] = []

    def deny(*, realpath_of: tuple[Path, ...] = (), lstat_of: tuple[Path, ...] = ()) -> None:
        bad_real = {os.path.normcase(str(t)) for t in realpath_of}
        bad_lstat = {os.path.normcase(str(t)) for t in lstat_of}

        def realpath(path, *args, strict=False, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            if strict and os.path.normcase(os.fspath(path)) in bad_real:
                fired.append("realpath")
                raise PermissionError(13, "injected: access denied")
            return real_realpath(path, *args, strict=strict, **kwargs)

        def lstat(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            if os.path.normcase(os.fspath(path)) in bad_lstat:
                fired.append("lstat")
                raise PermissionError(13, "injected: access denied")
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(os.path, "realpath", realpath)
        monkeypatch.setattr(os, "lstat", lstat)

    target = tmp_path / "a" / "c"
    for kind, inject in (("the parent (strict lookup)", {"realpath_of": (tmp_path / "a",)}),
                         ("a component on the way (lstat)", {"lstat_of": (tmp_path / "a",)}),
                         ("the path itself (lstat)", {"lstat_of": (target,)})):
        fired.clear()
        deny(**inject)
        with pytest.raises(PermissionError):
            lanes.canonical_host_path_checked(target)
        assert fired, kind
        monkeypatch.undo()


def _lstat_follows_a_trailing_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX `lstat("link/")` and `lstat("link/.")` follow the link, so a dangling link answers "not found". Windows
    does not, so this makes `os.lstat` behave the POSIX way for such raw strings, to reproduce 654-R26-1 on any
    platform. (On POSIX it changes nothing.)"""
    real_lstat, real_stat = os.lstat, os.stat

    def lstat(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        text = os.fspath(path)
        if isinstance(text, str) and text.endswith(("/", "\\", "/.", "\\.")):
            return real_stat(path, *args, **kwargs)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
@pytest.mark.parametrize("suffix", ["/", "/.", "//", "/./"],
                         ids=["slash", "slash-dot", "double-slash", "slash-dot-slash"])
def test_a_raw_suffix_after_a_dangling_link_is_unknown_not_the_link_name(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, action: str,
        suffix: str) -> None:
    """654-R26-1 (codex-rev): `link/.` and `link/` follow the link when looked up, but only the link's own name was
    returned; the final entry was never checked, so the marker's folder read as "a different folder". The raw string
    goes to the marker as it is (no `Path(...)`, which would erase the suffix)."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "rawsuffix")
    target, alias = tmp_path / "target", tmp_path / "alias"
    target.mkdir()
    _link_to(alias, target)
    target.rmdir()                                                       # the alias dangles
    _lstat_follows_a_trailing_suffix(monkeypatch)
    Store(root).write_launch_request({"request_id": "lr-rawsuffix", "state": "launched", "scope": {},
                                      "workspace_path": str(alias) + suffix})

    _held_back_or_refused(root, lane, "rawsuffix", action, capsys, "'lr-rawsuffix.json'")


windows_only = pytest.mark.skipif(os.name != "nt", reason="drive and stream syntax is Windows-only")


@windows_only
@pytest.mark.parametrize("separator", ["\\", "/"], ids=["backslash", "slash"])
@pytest.mark.parametrize("tail", ["{drive}ghost", "{drive}", "name:stream", "{drive}ghost{sep}x"],
                         ids=["drive-name", "drive-alone", "stream", "drive-name-then-child"])
def test_a_component_that_reads_as_a_drive_or_a_stream_is_refused(
        tmp_path: Path, separator: str, tail: str) -> None:
    """654-R27-1: put together again, a component such as `C:ghost` changes meaning (drive-relative). Raw strings, not
    `Path(...)`: the check must not be fooled by how a path object would read them."""
    (tmp_path / "exists").mkdir()
    raw = str(tmp_path / "exists") + separator + tail.format(drive=tmp_path.drive, sep=separator)

    with pytest.raises(OSError):
        lanes.canonical_host_path_checked(raw)


@windows_only
def test_an_ordinary_absent_leaf_is_still_resolved_next_to_the_drive_like_refusals(tmp_path: Path) -> None:
    (tmp_path / "exists").mkdir()
    raw = str(tmp_path / "exists") + os.sep + "ghost"

    assert lanes.canonical_host_path_checked(raw) == lanes.canonical_host_path(raw)


@windows_only
@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_a_marker_naming_the_worktree_through_a_drive_like_component_is_unknown_not_unrelated(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, action: str) -> None:
    """654-R27-1 (codex-rev), native Windows, no mocks: `<parent>\\C:<name>` with the current folder at the
    worktrees' parent names the lane's own checkout for the plain resolver. The checked resolver used to name
    `<parent>\\<name>` (it took `C:` for a drive when rebuilding the path), the check said idle, and abandon removed the
    checkout.

    Whether the plain resolver really names the lane's checkout depends on the Python. It does where `ntpath` reads the
    `C:name` piece as drive-relative (CPython 3.14 on Windows, as run here). It does not on CPython 3.13 (3.13.17 in
    CI windows/3.13, 3.13.18 standalone): there the plain resolver keeps `<empty parent>\\c:<name>` as a different,
    absent path and the hole cannot happen. So no precondition about the plain resolver is claimed, and the Python's
    version is not read. On both, the checked resolver refuses the `C:` component, abandon keeps the checkout pending
    and the acknowledgement exits 2 with the record unchanged; on 3.14 that is the regression for the hole."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "driveish")
    wt = Path(lane["worktree_path"])
    empty_parent = tmp_path / "empty-raw-parent"
    empty_parent.mkdir()                                                 # nothing in it: the rebuilt entry is absent
    workspace = f"{empty_parent}{os.sep}{wt.drive}{wt.name}"
    monkeypatch.chdir(wt.parent)
    # No claim about the plain resolver here (see the docstring): on 3.14 it names the lane's checkout, on 3.13.17 it
    # does not. The guard is the same on both: the `C:` component is refused, not read as a drive.
    with pytest.raises(OSError):
        lanes.canonical_host_path_checked(workspace)
    Store(root).write_launch_request({"request_id": "lr-driveish", "state": "launched", "scope": {},
                                      "workspace_path": workspace})

    _held_back_or_refused(root, lane, "driveish", action, capsys, "'lr-driveish.json'")


@windows_only
@pytest.mark.parametrize("prefix", ["\\\\?\\", "\\\\.\\", "//?/", "//./"],
                         ids=["extended", "device", "extended-slashes", "device-slashes"])
@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_an_extended_or_device_spelling_of_the_checkout_is_unknown_not_a_different_folder(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str, prefix: str) -> None:
    """654-R28-1 (codex-rev), native Windows, no mocks: a marker names the lane's own checkout as `\\\\?\\C:\\...`. The
    token of that spelling (`//?/c:/...`) never equals the lane's (`c:/...`), so the check said idle and abandon
    removed the checkout. No prefix is stripped or translated: the spelling is refused ("could not be established")."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "extended")
    wt = Path(lane["worktree_path"])
    store = Store(root)
    store.write_launch_request({"request_id": "lr-ordinary", "state": "launched", "scope": {},
                                "workspace_path": str(wt)})
    assert cli._lane_worktree_launch_use(store, lane).startswith(cli._LANE_WORKTREE_BUSY)    # the busy control
    assert store.archive_launch_request("lr-ordinary", {"request_id": "lr-ordinary", "terminal_state": "completed"})
    store.write_launch_request({"request_id": "lr-extended", "state": "launched", "scope": {},
                                "workspace_path": prefix + str(wt)})

    _held_back_or_refused(root, lane, "extended", action, capsys, "'lr-extended.json'")


@windows_only
@pytest.mark.parametrize("prefix", ["\\\\?\\", "\\\\.\\"], ids=["extended", "device"])
def test_an_extended_spelling_in_a_supervisor_record_is_unknown(
        tmp_path: Path, capsys: pytest.CaptureFixture, prefix: str) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "extrec")
    state = {"agents": {"claude-dev": {"cwd": prefix + lane["worktree_path"]}}}
    (Store(root).dir / "supervisor-state.json").write_text(json.dumps(state), encoding="utf-8")

    _held_back_or_refused(
        root, lane, "extrec", "abandon", capsys, "record 'claude-dev', field 'cwd' cannot be resolved")


def test_an_extended_spelling_of_the_lanes_own_worktree_is_unknown(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "ownext")
    store = Store(root)
    spelled = {**lane, "worktree_path": "\\\\?\\" + lane["worktree_path"]}

    reason = cli._lane_worktree_launch_use(store, spelled)

    assert reason is not None and "could not be established" in reason


def test_the_extended_prefix_is_refused_for_any_slash_and_case_but_a_unc_name_is_not() -> None:
    for raw in ("\\\\?\\C:\\x", "//?/C:/x", "\\\\.\\C:\\x", "//./c:/x", "/\\?/C:/x"):
        with pytest.raises(OSError):
            lanes.canonical_host_path_checked(raw)
    assert lanes._EXTENDED_PREFIX.match("\\\\server\\share\\x") is None
    assert lanes._EXTENDED_PREFIX.match("//host/share/x") is None
    assert lanes._EXTENDED_PREFIX.match("C:\\x") is None


def test_two_spellings_of_one_folder_that_differ_only_in_case_name_the_same_folder() -> None:
    """The comparison is case-insensitive on every platform: a case difference between two spellings of one folder is a
    match (a false match between two folders that differ only in case merely refuses)."""
    assert cli._lane_same_folder("/Work/Tree", "/work/Tree", "/work/tree", "/work/tree") is True
    assert cli._lane_same_folder("/work/other", "/work/other", "/work/tree", "/work/tree") is False


def test_a_name_that_samefile_says_is_the_same_existing_folder_counts_even_if_the_tokens_differ(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A spelling the tokens do not equate (here: forced to differ) is still a match when both folders exist and
    `os.path.samefile` says so; a `samefile` that fails or says "different" decides nothing by itself."""
    folder = tmp_path / "tree"
    folder.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    assert cli._lane_same_folder(str(folder), "a-token", str(folder), "another-token") is True      # same folder
    assert cli._lane_same_folder(str(other), "a-token", str(folder), "another-token") is False      # different
    assert cli._lane_same_folder(str(tmp_path / "missing"), "a-token", str(folder), "another-token") is False

    def broken(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise PermissionError(13, "injected: access denied")

    monkeypatch.setattr(os.path, "samefile", broken)
    assert cli._lane_same_folder(str(folder), "a-token", str(folder), "another-token") is False   # no proof either way


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_a_dotdot_path_through_a_dangling_link_and_a_file_is_unknown_not_unrelated(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str) -> None:
    """654-R25-1 (codex-rev): `alias/../file/../other` with `alias` pointing at nothing and `file` a regular file.
    The standard POSIX resolver stops at the file with ENOTDIR; the old walk lexically gave `.../other`, which the
    check read as "a different folder"."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "enotdir")
    target, alias, plain_file = tmp_path / "target", tmp_path / "alias", tmp_path / "file"
    target.mkdir()
    _link_to(alias, target)
    target.rmdir()
    plain_file.write_text("x", encoding="utf-8")
    sep = os.sep
    workspace = f"{alias}{sep}..{sep}file{sep}..{sep}other"
    Store(root).write_launch_request({"request_id": "lr-enotdir", "state": "launched", "scope": {},
                                      "workspace_path": workspace})

    _held_back_or_refused(root, lane, "enotdir", action, capsys, "'lr-enotdir.json'")


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_a_marker_naming_a_dangling_alias_of_a_moved_checkout_is_unknown(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str) -> None:
    """The lane's checkout is moved; a launched marker names a junction that pointed at its old folder. The alias
    dangles now. It is not followed (that was emulation): the check says it could not be established, so the lane
    is held and the acknowledgement refused."""
    (tmp_path / "store").mkdir()
    root, base = _repo(tmp_path / "store")
    lane = _assign_worktree(root, base, "aliasmoved")
    old = Path(lane["worktree_path"])
    alias = tmp_path / "alias-link"
    _link_to(alias, old)
    store = Store(root)
    store.write_launch_request({"request_id": "lr-moved", "state": "launched", "scope": {},
                                "workspace_path": str(alias)})
    assert cli._lane_worktree_launch_use(store, lane).startswith(cli._LANE_WORKTREE_BUSY)     # alive: it matches
    live = tmp_path / "live" / "elsewhere"
    live.parent.mkdir()
    assert _git_rc(root, "worktree", "move", str(old), str(live)).returncode == 0              # the alias now dangles
    before = lanes.load_lanes(store)
    capsys.readouterr()

    if action == "acknowledge":
        assert _run_raw(["lane", "abandon", "--id", "aliasmoved", ACK], root) == 2
        err = capsys.readouterr().err
        assert "could not be established" in err and "'lr-moved.json'" in err
        assert lanes.load_lanes(store) == before
    else:
        assert _run_raw(["lane", "abandon", "--id", "aliasmoved"], root) == 0
        record = lanes.load_lanes(store)["lanes"]["aliasmoved"]
        assert record["worktree_state"] == lanes.STATUS_CLEANUP_PENDING
        assert "could not be established" in record["worktree_cleanup_error"]
        assert "'lr-moved.json'" in record["worktree_cleanup_error"]
    assert live.exists()


@pytest.mark.parametrize("action", ["abandon", "acknowledge"])
def test_a_launch_requests_folder_and_a_state_file_that_are_truly_missing_stay_idle(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "never")
    wt = Path(lane["worktree_path"])
    store = Store(root)
    assert not os.path.lexists(store.launch_requests_dir) and not os.path.lexists(store.dir / "supervisor-state.json")
    capsys.readouterr()

    if action == "abandon":
        assert _run_raw(["lane", "abandon", "--id", "never"], root) == 0
        assert not wt.exists()
    else:
        shutil.rmtree(wt)
        assert _run_raw(["lane", "abandon", "--id", "never", ACK], root) == 0


def test_a_marker_that_vanishes_while_the_folder_is_read_is_skipped(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """Archived by the supervisor between listing and reading: gone is not damaged."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "vanish")
    wt = Path(lane["worktree_path"])
    store = Store(root)
    store.write_launch_request({"request_id": "lr-vanish", "state": "launched", "lane_id": "other"})

    def archived_meanwhile(self, request_id):  # noqa: ANN001, ANN202
        (self.launch_requests_dir / f"{request_id}.json").unlink()
        return None

    monkeypatch.setattr(Store, "read_launch_request", archived_meanwhile)
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "vanish"], root) == 0

    assert not wt.exists()


def test_a_launch_that_is_found_wins_over_a_record_that_cannot_be_read(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "bothwrong")
    store = Store(root)
    store.write_launch_request({"request_id": "lr-bad2", "state": "queued", "lane_id": "other"})
    (store.launch_requests_dir / "lr-bad2.json").write_text("{not json", encoding="utf-8")
    store.write_launch_request({"request_id": "lr-ok", "state": "queued", "lane_id": "bothwrong"})
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "bothwrong"], root) == 0

    error = lanes.load_lanes(store)["lanes"]["bothwrong"]["worktree_cleanup_error"]
    assert "active or pending launch" in error and Path(lane["worktree_path"]).exists()


def test_a_launched_marker_keeps_the_worktree_through_abandon_and_gc_until_it_is_archived(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The same guard decides plain `lane abandon` and `lane gc`, so a `launched` marker holds them back too."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "launchedgc")
    wt = Path(lane["worktree_path"])
    store = Store(root)
    store.write_launch_request({"request_id": "lr-launched-gc", "state": "launched", "lane_id": "launchedgc"})

    assert _run_raw(["lane", "abandon", "--id", "launchedgc"], root) == 0
    saved = lanes.load_lanes(store)["lanes"]["launchedgc"]
    assert saved["worktree_state"] == lanes.STATUS_CLEANUP_PENDING and wt.exists()
    capsys.readouterr()

    assert _run_raw(["lane", "gc", "--json"], root) == 0                       # a listing: nothing is deleted
    item = next(i for i in json.loads(capsys.readouterr().out)["items"] if i["lane_id"] == "launchedgc")
    assert item["worktree_remove_safe"] is False and "active or pending launch" in item["worktree_reason"]
    assert wt.exists()

    assert store.archive_launch_request("lr-launched-gc", {
        "request_id": "lr-launched-gc", "terminal_state": "failed"})
    assert _run_raw(["lane", "gc", "--delete", "--json"], root) == 0
    item = next(i for i in json.loads(capsys.readouterr().out)["items"] if i["lane_id"] == "launchedgc")
    assert item["worktree_removed"] is True and not wt.exists()


@pytest.mark.parametrize("finish", ["archived", "failed"])
@pytest.mark.parametrize("action", ["acknowledge", "abandon", "gc"])
def test_a_finished_launch_never_blocks_a_lane(
        tmp_path: Path, capsys: pytest.CaptureFixture, action: str, finish: str) -> None:
    """PR 654 round 9: once a launch has ended (its marker archived by the supervisor, or marked failed) the lane is
    free again for the acknowledgement, plain `lane abandon` and `lane gc`."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "finished")
    wt = Path(lane["worktree_path"])
    store = Store(root)
    store.write_launch_request({"request_id": "lr-finished", "state": "launched", "lane_id": "finished"})
    if action == "gc":
        assert _run_raw(["lane", "abandon", "--id", "finished"], root) == 0      # held back while the launch runs
        assert wt.exists()
    if finish == "archived":
        assert store.archive_launch_request("lr-finished", {"request_id": "lr-finished", "terminal_state": "completed"})
    else:
        assert store.update_launch_request("lr-finished", {"state": "failed"})
    capsys.readouterr()

    if action == "acknowledge":
        shutil.rmtree(wt)
        assert _run_raw(["lane", "abandon", "--id", "finished", ACK], root) == 0
        record = lanes.load_lanes(store)["lanes"]["finished"]
        assert record["worktree_state"] == lanes.STATUS_CLEANUP_ACKNOWLEDGED
    elif action == "abandon":
        assert _run_raw(["lane", "abandon", "--id", "finished"], root) == 0
        assert lanes.load_lanes(store)["lanes"]["finished"]["worktree_state"] == lanes.STATUS_ABANDONED
        assert not wt.exists()
    else:
        assert _run_raw(["lane", "gc", "--delete", "--json"], root) == 0
        item = next(i for i in json.loads(capsys.readouterr().out)["items"] if i["lane_id"] == "finished")
        assert item["worktree_removed"] is True and not wt.exists()


def test_the_refusal_for_an_acknowledged_lane_whose_folder_reappears_points_to_a_manual_look(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """PR 654 round 9: plain `lane abandon` deliberately does nothing for an acknowledged lane, so the refusal must
    not send the person to it."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "reappears")
    _fail_one_save_then_recover(root, "reappears", monkeypatch)
    assert _run_raw(["lane", "abandon", "--id", "reappears", ACK], root) == 0
    Path(lane["worktree_path"]).mkdir(parents=True)                  # the folder is back
    store = Store(root)
    before = lanes.load_lanes(store)
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "reappears", ACK], root) == 2

    err = capsys.readouterr().err
    assert "exists again" in err and "inspect" in err and "without the option" not in err
    assert lanes.load_lanes(store) == before


def test_the_acknowledgement_is_refused_for_a_relative_stored_path(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A relative path is "gone" or not depending on the folder the command runs in; the option will not guess."""
    (tmp_path / "store").mkdir()
    root, base = _repo(tmp_path / "store")
    lane = _assign_worktree(root, base, "relative")
    store = Store(root)
    data = lanes.load_lanes(store)
    data["lanes"]["relative"]["worktree_path"] = os.path.relpath(lane["worktree_path"], root)
    lanes.save_lanes(store, data)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")                         # here the relative path points at nothing
    before = lanes.load_lanes(store)
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "relative", ACK], root) == 2

    assert "not absolute" in capsys.readouterr().err
    assert lanes.load_lanes(store) == before and Path(lane["worktree_path"]).exists()


@pytest.mark.parametrize("path, expected", [
    (r"\repo\worktree", False), ("/repo/worktree", False),
    (r"C:repo\worktree", False), ("repo/worktree", False),
    (r"C:\repo\worktree", True), ("C:/repo/worktree", True),
    (r"\\server\share\worktree", True), ("//server/share/worktree", True),
])
def test_windows_absolute_worktree_paths_need_a_drive_and_a_root(path, expected):
    assert lanes._windows_path_is_absolute(path) is expected


@windows_only
@pytest.mark.parametrize("separator", ["\\", "/"])
def test_the_acknowledgement_refuses_a_windows_root_without_a_drive(
        tmp_path, monkeypatch, capsys, separator):
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "rooted")
    store = Store(root)
    data = lanes.load_lanes(store)
    stored = str(Path(lane["worktree_path"])).removeprefix(tmp_path.drive).replace("\\", separator) + "-missing"
    data["lanes"]["rooted"]["worktree_path"] = stored
    lanes.save_lanes(store, data)
    before = lanes.lanes_path(store).read_bytes()
    original_isabs = os.path.isabs
    # Reproduce supported CPython 3.10-3.12 even on a newer Windows interpreter.
    monkeypatch.setattr(os.path, "isabs", lambda path: True if path == stored else original_isabs(path))
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "rooted", ACK], root) == 2

    err = capsys.readouterr().err
    assert "not absolute" in err and repr(stored) in err
    assert lanes.lanes_path(store).read_bytes() == before
    assert Path(lane["worktree_path"]).exists()


@windows_only
@pytest.mark.parametrize("separator", ["\\", "/"])
def test_the_checked_resolver_refuses_a_windows_root_without_a_drive(tmp_path, separator):
    stored = str(tmp_path / "absent").removeprefix(tmp_path.drive).replace("\\", separator)
    with pytest.raises(OSError, match="without a drive"):
        lanes.canonical_host_path_checked(stored)
    reason = cli._lane_worktree_launch_use(Store(tmp_path), {"worktree_path": stored})
    assert "could not be established" in reason


def test_lane_status_shows_a_cleanup_only_lane_without_reading_the_domain_registry(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """PR 654 review: with no active lane the registry and the epoch are not needed, so a registry that cannot be read
    must not stop `lane status` from printing the command that settles an abandoned lane's pending cleanup."""
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "cleanuponly")
    _fail_one_save_then_recover(root, "cleanuponly", monkeypatch)
    assert _run_raw(["lane", "abandon", "--id", "cleanuponly"], root) == 0     # abandoned, cleanup pending
    (Store(root).dir / "domains.json").write_text("{not json", encoding="utf-8")
    capsys.readouterr()

    assert _run_raw(["lane", "status"], root) == 0

    text = capsys.readouterr().out
    assert "cleanuponly" in text and f"lane abandon --id cleanuponly {ACK}" in text


def test_the_acknowledgement_is_refused_for_an_unknown_lane_and_for_a_lane_without_a_worktree(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    assert _run_raw([
        "lane", "assign", "--id", "plain", "--from", "lead", "--assignee", "dev", "--advisory", "--domain", "core",
        "--base", base, "--target", _branch(root), "--path", "src/core", "--no-worktree",
        "--worktree-waiver-reason", "ack fixture",
    ], root) == 0
    store = Store(root)
    before = lanes.load_lanes(store)
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "ghost", ACK], root) == 2
    assert "no lane 'ghost'" in capsys.readouterr().err
    assert _run_raw(["lane", "abandon", "--id", "plain", ACK], root) == 2
    assert "no worktree" in capsys.readouterr().err
    assert lanes.load_lanes(store) == before


def test_an_older_record_without_any_new_field_can_be_acknowledged_and_keeps_working(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """Records written before this option exist: abandoned, cleanup_pending, a reason, no canonical path, none of the
    new fields. The acknowledgement uses only what is there and guesses no history."""
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "older")
    store = Store(root)
    _fail_one_save_then_recover(root, "older", monkeypatch)
    data = lanes.load_lanes(store)
    record = data["lanes"]["older"]
    record.pop("worktree_toplevel_canonical", None)
    record["status"] = lanes.STATUS_ABANDONED
    record["worktree_state"] = lanes.STATUS_CLEANUP_PENDING
    record["worktree_cleanup_error"] = "could not read worktree toplevel (git rc=128)"
    lanes.save_lanes(store, data)
    capsys.readouterr()

    assert _run_raw(["lane", "abandon", "--id", "older"], root) == 0          # as before: still pending
    assert lanes.load_lanes(store)["lanes"]["older"]["worktree_state"] == lanes.STATUS_CLEANUP_PENDING
    assert _run_raw(["lane", "abandon", "--id", "older", ACK], root) == 0
    settled = lanes.load_lanes(store)["lanes"]["older"]
    assert settled["worktree_state"] == lanes.STATUS_CLEANUP_ACKNOWLEDGED and not settled.get("worktree_cleanup_error")


def test_gc_never_removes_a_worktree_that_turns_up_for_an_acknowledged_lane(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The person was wrong: the lane's branch is checked out in a worktree that was only moved. gc must not treat the
    acknowledgement as leave to remove it, even for an older record that has no canonical path to compare."""
    (tmp_path / "store").mkdir()
    root, base = _repo(tmp_path / "store")
    lane = _assign_worktree(root, base, "wrong")
    store = Store(root)
    data = lanes.load_lanes(store)
    data["lanes"]["wrong"].pop("worktree_toplevel_canonical", None)
    lanes.save_lanes(store, data)
    old = Path(lane["worktree_path"])
    live = tmp_path / "live" / "moved"
    live.parent.mkdir()
    assert _git_rc(root, "worktree", "move", str(old), str(live)).returncode == 0
    assert _run_raw(["lane", "abandon", "--id", "wrong", ACK], root) == 0
    capsys.readouterr()

    assert _run_raw(["lane", "gc", "--delete", "--json"], root) == 0

    item = next(i for i in json.loads(capsys.readouterr().out)["items"] if i["lane_id"] == "wrong")
    assert item["worktree_remove_safe"] is False and "acknowledged" in item["worktree_reason"]
    assert item["branch_delete_safe"] is False
    assert live.exists() and _branch_exists(root, "lane/wrong")


def test_the_help_says_plainly_that_the_option_records_a_persons_acknowledgement(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit):
        cli.main(["lane", "abandon", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert ACK in text
    assert "records your acknowledgement" in text and "not verified" in text
    assert "never removes a checkout and never deletes a branch" in text
    assert "cannot be combined with --delete-branch" in text


def test_lane_status_lists_abandoned_lanes_whose_cleanup_is_still_pending(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "stuck")
    _fail_one_save_then_recover(root, "stuck", monkeypatch)
    assert _run_raw(["lane", "abandon", "--id", "stuck"], root) == 0           # abandoned, cleanup pending
    capsys.readouterr()

    assert _run_raw(["lane", "status"], root) == 0
    text = capsys.readouterr().out
    assert "stuck" in text and "cleanup pending" in text and ACK in text
    assert _run_raw(["lane", "status", "--json"], root) == 0
    assert json.loads(capsys.readouterr().out) == []                          # the JSON shape is unchanged


def test_abandoning_an_already_abandoned_lane_still_succeeds(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The documented policy: a lane that has a record can be abandoned again (it retries a cleanup that did
    not finish) and exits 0; only a lane with no record at all exits 2."""
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "twice")
    assert _run_raw(["lane", "abandon", "--id", "twice"], root) == 0
    assert _run_raw(["lane", "abandon", "--id", "twice"], root) == 0
    out = capsys.readouterr().out
    assert out.count("abandoned lane twice") == 2


def test_lane_abandon_help_states_the_exit_codes(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit):
        cli.main(["lane", "abandon", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "Exit 0" in text and "Exit 2" in text and "no record" in text


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

        def wait(self, timeout=None):  # noqa: ANN001
            return -9

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_kw: StuckProc())
    with pytest.raises(cli.GitWriteError, match="could not be reaped"):
        cli._git_write(
            tmp_path, ["worktree", "add", "-b", "lane/safe", "--",
                       str(tmp_path / "wt2"), full_sha], timeout=0.01)
    assert seen["killed"] is True


def test_m12_git_write_kills_and_reaps_on_base_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # A termination signal (or any other BaseException) firing while
    # communicate() is blocked previously skipped straight past the
    # TimeoutExpired-only cleanup above and left the child git process
    # running, unreaped.
    seen = {}

    class SignaledProc:
        def __init__(self) -> None:
            self.calls = 0
            self._returncode = None

        def poll(self):
            return self._returncode

        def kill(self):
            seen["killed"] = True
            self._returncode = -9

        def communicate(self, timeout=None):  # noqa: ANN001
            self.calls += 1
            if self.calls == 1:
                raise SystemExit(143)
            return "", ""

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_kw: SignaledProc())
    with pytest.raises(SystemExit):
        cli._git_write(
            tmp_path, ["worktree", "add", "-b", "lane/safe", "--",
                       str(tmp_path / "wt3"), "a" * 40])
    assert seen["killed"] is True


def test_m12_git_write_falls_back_to_wait_when_retry_communicate_hangs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # PR 98 connector, round 9: a hook or descendant holding the captured
    # stdout/stderr pipe handles open can keep the retry communicate()
    # blocked past its own 5s timeout even though the killed process is
    # already dead. wait() does not touch the pipes at all, so it still
    # completes once the process is actually gone - fall back to it
    # instead of silently giving up on the reap.
    #
    # This is why test_m12_git_write_kills_and_reaps_on_base_exception
    # above is not enough on its own, as asked: its fake kill() marks the
    # process reaped SYNCHRONOUSLY and its retry communicate() never
    # times out, so that test would still pass unchanged with the
    # swallowed-second-timeout bug present - it proves kill() was called,
    # not that reap completes under the failure mode this finding
    # describes. This test drives the retry communicate() into a second
    # TimeoutExpired specifically, so it fails on the old code (nothing
    # calls wait(), the process is left with poll() still None) and
    # passes on the fix.
    seen: dict = {}

    class StuckRetryProc:
        def __init__(self) -> None:
            self.communicate_calls = 0
            self._returncode = None

        def poll(self):
            return self._returncode

        def kill(self):
            seen["killed"] = True

        def communicate(self, timeout=None):  # noqa: ANN001
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise SystemExit(143)
            raise subprocess.TimeoutExpired("git", timeout)

        def wait(self, timeout=None):  # noqa: ANN001
            seen["wait_timeout"] = timeout
            self._returncode = -9
            return self._returncode

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_kw: StuckRetryProc())
    with pytest.raises(SystemExit):
        cli._git_write(
            tmp_path, ["worktree", "add", "-b", "lane/safe", "--",
                       str(tmp_path / "wt4"), "a" * 40])
    assert seen["killed"] is True
    assert seen.get("wait_timeout") is not None


def test_m12_git_write_kill_raising_does_not_replace_the_owner_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Round 11 connector finding, sibling of the same pattern reported for
    # wrapper/run.py: kill() can itself raise (PermissionError on Windows,
    # ProcessLookupError if the child already exited). Unguarded, that
    # secondary error would replace the SystemExit being cleaned up after
    # and skip the reap fallback entirely.
    seen: dict = {}

    class RaisingKillProc:
        def __init__(self) -> None:
            self.calls = 0
            self._returncode = None

        def poll(self):
            return self._returncode

        def kill(self):
            seen["kill_called"] = True
            raise PermissionError("simulated: process handle already closing")

        def communicate(self, timeout=None):  # noqa: ANN001
            self.calls += 1
            if self.calls == 1:
                raise SystemExit(143)
            return "", ""

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_kw: RaisingKillProc())
    with pytest.raises(SystemExit):
        cli._git_write(
            tmp_path, ["worktree", "add", "-b", "lane/safe", "--",
                       str(tmp_path / "wt5"), "a" * 40])
    assert seen["kill_called"] is True


def test_m12_git_write_kill_raising_does_not_replace_the_timeout_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Same finding, the routine (non-BaseException) timeout branch: kill()
    # raising here would replace the intended GitWriteError with a bare
    # PermissionError, breaking any caller that catches GitWriteError
    # specifically.
    seen: dict = {}

    class RaisingKillProc:
        def kill(self):
            seen["kill_called"] = True
            raise PermissionError("simulated: process handle already closing")

        def communicate(self, timeout=None):  # noqa: ANN001
            raise subprocess.TimeoutExpired("git", timeout)

        def wait(self, timeout=None):  # noqa: ANN001
            return -9

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_kw: RaisingKillProc())
    with pytest.raises(cli.GitWriteError, match="could not be reaped"):
        cli._git_write(
            tmp_path, ["worktree", "add", "-b", "lane/safe", "--",
                       str(tmp_path / "wt6"), "a" * 40], timeout=0.01)
    assert seen["kill_called"] is True


def test_m12_git_write_routine_timeout_reaps_via_wait_on_second_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Round 12 connector finding: a THIRD exit from _git_write's timeout
    # handling, distinct from the BaseException branch fixed in round 9
    # and the kill()-guard fixed in round 11 - on the ROUTINE timeout
    # retry, a second TimeoutExpired raised immediately without ever
    # calling wait(), so a killed git could still go unreaped while this
    # function returned (by raising), contradicting its own docstring
    # contract and leaving a lane mutation's lock state uncertain.
    seen: dict = {}

    class StuckRoutineRetryProc:
        def __init__(self) -> None:
            self.communicate_calls = 0
            self._returncode = None

        def poll(self):
            return self._returncode

        def kill(self):
            seen["killed"] = True

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            raise subprocess.TimeoutExpired("git", timeout)

        def wait(self, timeout=None):  # noqa: ANN001
            seen["wait_timeout"] = timeout
            self._returncode = -9
            return self._returncode

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_kw: StuckRoutineRetryProc())
    with pytest.raises(cli.GitWriteError, match="could not be reaped"):
        cli._git_write(
            tmp_path, ["worktree", "add", "-b", "lane/safe", "--",
                       str(tmp_path / "wt7"), "a" * 40], timeout=0.01)
    assert seen["killed"] is True
    assert seen.get("wait_timeout") is not None


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


def test_recover_quarantines_invalid_current_v3_final_before_fresh_delivery(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    lane_id = "recover-invalid-v3"
    head = _advisory_delivery_fixture(root, base, lane_id)
    argv = ["lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head]
    store, lane_state = _leave_rebound_publish_pending(
        root, argv, lane_id, monkeypatch,
    )
    pending = lane_state["publish_pending"]
    prepared = Path(pending["prepared_artifact"])
    final = Path(pending["committed_artifact"])
    payload = json.loads(prepared.read_text(encoding="utf-8"))
    payload["artifact_state"] = lanes.ARTIFACT_COMMITTED
    payload["integrity_token"] = lanes.compute_integrity_token(store, payload)
    payload["delivered_by"] = "tampered-after-signing"
    final.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    rejected_bytes = final.read_bytes()

    assert _run_raw(argv, root) == 2
    assert final.read_bytes() == rejected_bytes
    assert _run_raw([
        "lane", "recover", "--id", lane_id,
        "--reason", "quarantine invalid current v3 final and reevaluate",
    ], root) == 0

    restored = lanes.load_lanes(store)["lanes"][lane_id]
    quarantine = Path(restored["delivery_recovery"]["quarantined_final"])
    assert restored["status"] == lanes.STATUS_ACTIVE
    assert not final.exists()
    assert quarantine.exists()
    assert quarantine.read_bytes() == rejected_bytes

    assert _run_raw(argv, root) == 0
    completed = lanes.load_lanes(store)["lanes"][lane_id]
    assert completed["publish_pending"] is False
    assert len(_committed_artifacts(store, lane_id)) == 1


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


def _deep_lane_repo(tmp_path: Path, final_length: int, lane_id: str) -> tuple[Path, str]:
    """A repo in a folder deep enough that lane_id's delivery records have final_length characters."""
    record_name_length = len(lane_id) + 51
    padding = final_length - len(str(tmp_path)) - 1 - len("/.agenttalk/lane-deliveries/") - record_name_length
    if padding < 1:
        pytest.skip(f"the test folder is too long to build a {final_length}-character record")
    deep = tmp_path / ("d" * padding)
    deep.mkdir()
    return _repo(deep)


def _limit_paths_like_windows(patch: pytest.MonkeyPatch) -> list[str]:
    """Make creating, linking and renaming a path of more than 259 characters fail the way
    Windows without long paths fails it; returns the refused paths."""
    refused: list[str] = []

    def check(*paths: object) -> None:
        for path in paths:
            if len(str(path)) > 259:
                refused.append(str(path))
                raise FileNotFoundError(3, "The system cannot find the path specified")

    def one_path(real):  # noqa: ANN001, ANN202
        def call(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            check(path)
            return real(path, *args, **kwargs)

        return call

    def two_paths(real):  # noqa: ANN001, ANN202
        def call(source, target, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            check(source, target)
            return real(source, target, *args, **kwargs)

        return call

    patch.setattr(os, "open", one_path(os.open))
    for name in ("replace", "rename", "link"):
        patch.setattr(os, name, two_paths(getattr(os, name)))
    return refused


@pytest.mark.parametrize("final_length", [217, 244])
def test_recover_quarantine_fits_when_renames_are_limited_to_260_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, final_length: int
) -> None:
    """#587: the rejected final's quarantine name repeated the record's name, so on Windows
    without long paths the move failed with "path not found" once it passed 259 characters.
    Only renames are limited here, and only after delivery. 217 is the record from the issue;
    244 is the longest record lane deliver can write once the store's integrity secret exists.
    A first delivery into a new store stops earlier: see the creation-limit test below."""
    lane_id = "long"
    root, base = _deep_lane_repo(tmp_path, final_length, lane_id)
    head = _advisory_delivery_fixture(root, base, lane_id)
    real_checkpoint = cli._lane_checkpoint_publication
    monkeypatch.setattr(
        cli,
        "_lane_checkpoint_publication",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("marker save crash")),
    )
    assert (
        _run_raw(
            [
                "lane",
                "deliver",
                "--id",
                lane_id,
                "--from",
                "dev",
                "--head",
                head,
            ],
            root,
        )
        == 2
    )
    monkeypatch.setattr(cli, "_lane_checkpoint_publication", real_checkpoint)
    store = Store(root)
    data = lanes.load_lanes(store)
    final = Path(data["lanes"][lane_id]["publish_pending"]["committed_artifact"])
    assert len(str(final)) == final_length
    record = final.read_bytes()
    data["lanes"][lane_id].pop("publish_pending")
    lanes.save_lanes(store, data)

    def limited(real):  # noqa: ANN001, ANN202
        def move(source, target, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            if len(str(target)) > 259:
                raise FileNotFoundError(3, "The system cannot find the path specified")
            return real(source, target, *args, **kwargs)

        return move

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", limited(os.replace))
        patch.setattr(os, "rename", limited(os.rename))
        assert (
            _run_raw(
                [
                    "lane",
                    "recover",
                    "--id",
                    lane_id,
                    "--reason",
                    "quarantine a final whose publication marker was lost",
                ],
                root,
            )
            == 0
        )
    recovered = lanes.load_lanes(store)["lanes"][lane_id]
    quarantine = Path(recovered["delivery_recovery"]["quarantined_final"])
    assert recovered["status"] == lanes.STATUS_ACTIVE
    assert not final.exists()
    assert quarantine.read_bytes() == record
    assert re.fullmatch(r"[0-9a-f]{16}\.rejected", quarantine.name)
    assert len(str(quarantine)) < len(str(final))


_PREPARED_COPY = r"long-[0-9a-f]{32}\.prepared\.json"


@pytest.mark.parametrize(
    ("final_length", "secret_exists", "blocked_by"),
    [
        (217, False, None),
        (242, False, None),
        (245, True, None),
        (251, False, None),
        (253, False, None),
        (253, True, None),
        (254, False, _PREPARED_COPY),
        (259, True, _PREPARED_COPY),
    ],
    ids=[
        "issue-record",
        "first-delivery-past-the-old-lock-limit",
        "delivery-past-the-old-prepared-copy-limit",
        "first-delivery-past-the-old-record-temp-limit",
        "longest-first-delivery",
        "longest-delivery",
        "first-delivery-prepared-copy-too-long",
        "deepest-record-prepared-copy-too-long",
    ],
)
def test_first_delivery_and_recovery_under_a_file_creation_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    final_length: int,
    secret_exists: bool,
    blocked_by: str | None,
) -> None:
    """#587 review and #593: with creating files limited too, not only renaming, from before
    the first delivery. Three temporary files used to be longer than the record: the
    integrity-secret lock's (record + 18 with this 4-character lane id, stopping a first
    delivery at 242), the prepared copy's (record + 15, stopping any delivery at 245) and the
    record's own (record + 9, at 251 to 253, after the delivery was recorded). All three now
    have short fixed-length names, so delivery fits up to a 253-character record. From 254
    the prepared copy itself (record + 6) is the longest file, and it is made before the
    delivery is recorded: delivery is refused with the lane untouched. Wherever delivery
    fits, recovery fits too."""
    lane_id = "long"
    root, base = _deep_lane_repo(tmp_path, final_length, lane_id)
    head = _advisory_delivery_fixture(root, base, lane_id)
    store = Store(root)
    if secret_exists:
        lanes._integrity_secret(store)
    deliver = ["lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head]
    with monkeypatch.context() as patch:
        refused = _limit_paths_like_windows(patch)
        delivered = _run_raw(deliver, root)
    assert [p.name for p in store.dir.rglob("*") if p.name.endswith((".prepare", ".unlink"))] == []
    if blocked_by is not None:
        assert delivered == 2
        assert re.fullmatch(blocked_by, Path(refused[0]).name)
        lane = lanes.load_lanes(store)["lanes"][lane_id]
        assert lane["status"] == lanes.STATUS_ACTIVE
        assert not lane.get("publish_pending")
        assert _prepared_artifacts(store, lane_id) == []
        assert _committed_artifacts(store, lane_id) == []
        return
    assert delivered == 0
    assert refused == []
    [final] = _committed_artifacts(store, lane_id)
    assert len(str(final)) == final_length
    record = final.read_bytes()
    data = lanes.load_lanes(store)
    data["lanes"][lane_id]["publish_pending"] = "corrupt"
    lanes.save_lanes(store, data)

    recover = ["lane", "recover", "--id", lane_id, "--reason", "quarantine a final whose marker is corrupt"]
    with monkeypatch.context() as patch:
        refused = _limit_paths_like_windows(patch)
        recovered_rc = _run_raw(recover, root)
    assert recovered_rc == 0
    assert refused == []
    recovered = lanes.load_lanes(store)["lanes"][lane_id]
    assert recovered["status"] == lanes.STATUS_ACTIVE
    assert not final.exists()
    assert Path(recovered["delivery_recovery"]["quarantined_final"]).read_bytes() == record


_CRASH_AFTER_LINKING = """
import os, sys
from pathlib import Path
from agenttalk import cli
root, head, crash_on = sys.argv[1:4]
real_link = os.link
def link_then_die(source, target, *args, **kwargs):
    real_link(source, target, *args, **kwargs)
    target = Path(target)
    if {"lock": target.name == ".worktree-integrity-secret.lock",
            "prepared": target.name.endswith(".prepared.json"),
            "record": target.parent.name == "lane-deliveries" and target.suffix == ".json"}[crash_on]:
        os._exit(91)
os.link = link_then_die
raise SystemExit(cli.main(["--root", root, "lane", "deliver", "--id", "crash", "--from", "dev", "--head", head]))
"""


@pytest.mark.parametrize(
    ("crash_on", "target_changes"),
    [("lock", False), ("prepared", False), ("record", False), ("record", True)],
    ids=["integrity-secret-lock", "prepared-copy", "record-retry-publishes", "record-retry-rejects"],
)
def test_a_crash_after_linking_leaves_no_second_name_as_evidence(
        tmp_path: Path, crash_on: str, target_changes: bool) -> None:
    """#603 review: the integrity-secret lock, the prepared copy and the record are each
    published by linking a private file, .<32 hex>.prepare, to the real name, so a process
    killed before it removes the private name leaves two names. A private name that is still
    the same file as the lock or the record is removed under the lock its writers hold (the
    lock's when it is taken; the record's transaction lock whether the retry publishes the
    record or sets it aside), and any other file is kept, here byte-for-byte copies under
    private-looking names. The prepared copy's is left: copies are prepared before any lock,
    so no delivery may remove another's. The close check accepts a record only at its own
    committed name, so no leftover counts as evidence."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "crash")
    head = _commit(Path(lane["worktree_path"]), "src/core/a.py", "base\nlane candidate\n")
    src = str(Path(lanes.__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")]))}
    crashed = subprocess.run([sys.executable, "-c", _CRASH_AFTER_LINKING, str(root), head, crash_on],
                             capture_output=True, env=env, check=False)
    assert crashed.returncode == 91, crashed.stderr
    store = Store(root)
    ddir = store.dir / "lane-deliveries"
    if crash_on == "lock":
        partner = ddir / ".worktree-integrity-secret.lock"
    elif crash_on == "prepared":
        [partner] = (ddir / ".prepared").glob("crash-*.prepared.json")
    else:
        partner = Path(lanes.load_lanes(store)["lanes"]["crash"]["publish_pending"]["committed_artifact"])
    [alias] = [p for p in partner.parent.glob(".*.prepare") if os.path.samefile(p, partner)]
    (ddir / ".prepared").mkdir(exist_ok=True)
    decoys = [ddir / f".{'d' * 32}.prepare", ddir / ".prepared" / f".{'e' * 32}.prepare"]
    for decoy in decoys:
        decoy.write_bytes(partner.read_bytes())
    if target_changes:
        _commit(root, "src/core/a.py", "base\ntarget changed\n")

    retried = _run_raw(["lane", "deliver", "--id", "crash", "--from", "dev", "--head", head], root)
    assert alias.exists() == (crash_on == "prepared")
    assert all(decoy.read_bytes() for decoy in decoys)
    if target_changes:
        assert retried == 3
        assert lanes.load_lanes(store)["lanes"]["crash"]["status"] == lanes.STATUS_ACTIVE
        assert not partner.exists()
        [rejected] = (ddir / ".recovery-quarantine").glob("*.rejected")
        assert os.lstat(rejected).st_nlink == 1
    else:
        assert retried == 0
        [final] = _committed_artifacts(store, "crash")
        assert os.lstat(final).st_nlink == 1
        assert cli._close_worktree_eval(store, {"lane_delivery_artifact": str(final)})["status"] == "verified"
    for leftover in [alias, *ddir.glob(".*.prepare"), *(ddir / ".prepared").glob(".*.prepare")]:
        assert cli._close_worktree_eval(store, {"lane_delivery_artifact": str(leftover)})["status"] == "unverified"


def _deliverable_worktree_lane(root: Path, base: str, lane_id: str) -> str:
    """A release lane on its own path, so several can deliver from one store."""
    assert _run_raw([
        "lane", "assign", "--id", lane_id, "--from", "lead", "--assignee", "dev",
        "--domain", "core", "--base", base, "--target", _branch(root),
        "--path", f"src/core/{lane_id}", "--worktrees-root", str(root / ".worktrees"),
    ], root) == 0
    worktree = Path(lanes.load_lanes(Store(root))["lanes"][lane_id]["worktree_path"])
    (worktree / "src" / "core" / lane_id).mkdir(parents=True)
    return _commit(worktree, f"src/core/{lane_id}/x.py", "x\n")


@pytest.mark.parametrize("same_lane", [True, False], ids=["same-lane", "other-lane"])
def test_a_delivery_never_touches_another_deliverys_prepared_copy_in_flight(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_lane: bool) -> None:
    """#603 merge-gate review: deliveries write their prepared copies before taking any lock,
    so two can be publishing at once, in one lane or two. Here a second delivery runs up to
    and including writing its own prepared copy (then stops) while the first is between
    linking its prepared copy and removing the private name, and any removal of the first
    one's file is held back as if its remover were descheduled. Nothing may touch the first
    delivery's file, and the first delivery completes."""
    root, base = _repo(tmp_path)
    first = _deliverable_worktree_lane(root, base, "first")
    second_id = "first" if same_lane else "second"
    second = first if same_lane else _deliverable_worktree_lane(root, base, "second")
    deliver = [["lane", "deliver", "--id", "first", "--from", "dev", "--head", first],
               ["lane", "deliver", "--id", second_id, "--from", "dev", "--head", second]]
    real_link, real_unlink = os.link, os.unlink
    prepared_links: list[Path] = []
    held_back: list[str] = []
    results: list[int] = []

    def link(source, target, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        real_link(source, target, *args, **kwargs)
        if Path(target).name.endswith(".prepared.json"):
            prepared_links.append(Path(target))
            if len(prepared_links) == 1:
                results.append(_run_raw(deliver[1], root))
            elif len(prepared_links) == 2:
                raise OSError("the second delivery stops after preparing")

    def unlink(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if (prepared_links and not results and str(path).endswith(".unlink")
                and os.path.samefile(path, prepared_links[0])):
            held_back.append(str(path))
            return None
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "unlink", unlink)
    results.insert(0, _run_raw(deliver[0], root))
    monkeypatch.undo()
    for path in held_back:
        os.unlink(path)

    assert len(prepared_links) == 2
    assert held_back == []
    assert results == [0, 2]
    assert len(_committed_artifacts(Store(root), "first")) == 1


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


@pytest.mark.parametrize(
    ("message_change", "expected_rc"),
    [("ordinary", 0), ("barrier_create", 3), ("barrier_mutate", 3)],
)
def test_message_change_after_pending_save_only_invalidates_verdict_evidence(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        message_change: str, expected_rc: int) -> None:
    root, base = _repo(tmp_path)
    lane_id = f"post-save-{message_change}"
    store = Store(root)
    barrier_path = None
    if message_change == "barrier_mutate":
        barrier = store.send(
            sender="lead", recipient="lead", body="release barrier",
            meta={"barrier": {"version": 1, "scope": "global", "type": "release"}},
        )
        barrier_path = store.messages_dir / f"{barrier.id}.json"
    head = _advisory_delivery_fixture(root, base, lane_id)
    real_boundary = cli._lane_prepare_publication
    changed = False

    def change_message_at_publication_boundary(target_store, target_lane_id):  # noqa: ANN001
        nonlocal changed
        if not changed:
            changed = True
            saved = lanes.load_lanes(target_store)["lanes"][target_lane_id]
            assert saved["status"] == lanes.STATUS_DELIVERED
            assert isinstance(saved["publish_pending"], dict)
            if message_change == "ordinary":
                target_store.send(
                    sender="lead", recipient="dev", kind="note",
                    body="ordinary traffic after the pending journal save",
                )
            elif message_change == "barrier_create":
                target_store.send(
                    sender="lead", recipient="lead", body="new release barrier",
                    meta={
                        "barrier": {
                            "version": 1, "scope": "global", "type": "release",
                        },
                    },
                )
            else:
                assert barrier_path is not None
                payload = json.loads(barrier_path.read_text(encoding="utf-8"))
                payload["body"] = "mutated release barrier"
                barrier_path.write_text(json.dumps(payload), encoding="utf-8")
        return real_boundary(target_store, target_lane_id)

    monkeypatch.setattr(cli, "_lane_prepare_publication", change_message_at_publication_boundary)
    assert _run_raw([
        "lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head,
    ], root) == expected_rc

    saved = lanes.load_lanes(store)["lanes"][lane_id]
    if expected_rc == 0:
        assert saved["publish_pending"] is False
        assert len(_committed_artifacts(store, lane_id)) == 1
    else:
        assert saved["status"] == lanes.STATUS_ACTIVE
        assert _committed_artifacts(store, lane_id) == []


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


def test_delivery_finalize_rebind_rejects_gate_red_after_prepare_returns(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    gates.set_gate(
        root, name="ci", status="green", severity="blocker", scope="release",
        actor="lead", evidence_source="automation_ci", evidence=["ci://green"],
        required=True,
    )
    lane_id = "post-prepare-gate"
    head = _advisory_delivery_fixture(root, base, lane_id)
    store = Store(root)
    real_prepare = cli._lane_prepare_publication
    changed = False

    def turn_gate_red_after_prepare(target_store, target_lane_id):  # noqa: ANN001
        nonlocal changed
        result = real_prepare(target_store, target_lane_id)
        if not changed:
            changed = True
            saved = lanes.load_lanes(target_store)["lanes"][target_lane_id]
            assert saved["publish_pending"]["terminal_rebound"] is True
            gates.set_gate(
                root, name="ci", status="red", severity="blocker", scope="release",
                actor="lead", evidence_source="local_command", reason="late failure",
            )
        return result

    monkeypatch.setattr(cli, "_lane_prepare_publication", turn_gate_red_after_prepare)
    assert _run_raw([
        "lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head,
        "--gate-scope", "release",
    ], root) == 3

    saved = lanes.load_lanes(store)["lanes"][lane_id]
    assert saved["status"] == lanes.STATUS_ACTIVE
    assert saved.get("publish_pending") is not False
    assert _committed_artifacts(store, lane_id) == []


def test_delivery_finalize_rebind_rejects_conflicting_target_after_prepare_returns(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    target_ref = _branch(root)
    lane_id = "post-prepare-target"
    lane = _assign_worktree(root, base, lane_id)
    _commit(
        Path(lane["worktree_path"]), "src/core/a.py", "base\nlane candidate\n",
    )
    _git(root, "checkout", "-q", "-b", "post-prepare-target-conflict", base)
    conflicting = _commit(root, "src/core/a.py", "base\ntarget conflict\n")
    _git(root, "checkout", "-q", target_ref)
    assert _git(root, "rev-parse", "HEAD").strip() == base
    store = Store(root)
    real_prepare = cli._lane_prepare_publication
    moved = False

    def move_target_after_prepare(target_store, target_lane_id):  # noqa: ANN001
        nonlocal moved
        result = real_prepare(target_store, target_lane_id)
        if not moved:
            moved = True
            saved = lanes.load_lanes(target_store)["lanes"][target_lane_id]
            assert saved["publish_pending"]["terminal_rebound"] is True
            assert _git_rc(
                root, "update-ref", f"refs/heads/{target_ref}", conflicting, base,
            ).returncode == 0
        return result

    monkeypatch.setattr(cli, "_lane_prepare_publication", move_target_after_prepare)
    assert _run_raw([
        "lane", "deliver", "--id", lane_id, "--from", "dev",
    ], root) == 3

    saved = lanes.load_lanes(store)["lanes"][lane_id]
    assert saved["status"] == lanes.STATUS_ACTIVE
    assert saved.get("publish_pending") is not False
    assert _committed_artifacts(store, lane_id) == []


def test_delivery_finalize_rebind_rejects_gate_red_after_final_publish(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    gates.set_gate(
        root, name="ci", status="green", severity="blocker", scope="release",
        actor="lead", evidence_source="automation_ci", evidence=["ci://green"],
        required=True,
    )
    lane_id = "post-publish-gate"
    head = _advisory_delivery_fixture(root, base, lane_id)
    store = Store(root)
    real_publish = lanes.publish_delivery_artifact
    changed = False

    def publish_then_turn_gate_red(*args, **kwargs):  # noqa: ANN002,ANN003
        nonlocal changed
        artifact = real_publish(*args, **kwargs)
        if not changed:
            changed = True
            gates.set_gate(
                root, name="ci", status="red", severity="blocker", scope="release",
                actor="lead", evidence_source="local_command", reason="late failure",
            )
        return artifact

    monkeypatch.setattr(lanes, "publish_delivery_artifact", publish_then_turn_gate_red)
    assert _run_raw([
        "lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head,
        "--gate-scope", "release",
    ], root) == 3

    saved = lanes.load_lanes(store)["lanes"][lane_id]
    assert saved["status"] == lanes.STATUS_ACTIVE
    assert saved.get("publish_pending") is not False
    assert _committed_artifacts(store, lane_id) == []


def test_completion_checkpoint_rebind_rejects_gate_red_at_entry(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    gates.set_gate(
        root, name="ci", status="green", severity="blocker", scope="release",
        actor="lead", evidence_source="automation_ci", evidence=["ci://green"],
        required=True,
    )
    lane_id = "checkpoint-entry-gate"
    lane = _assign_worktree(root, base, lane_id)
    head = _commit(
        Path(lane["worktree_path"]), "src/core/a.py", "base\ncheckpoint candidate\n",
    )
    store = Store(root)
    real_checkpoint = cli._lane_checkpoint_publication
    artifact_seen = None
    artifact_bytes = None

    def turn_gate_red_at_checkpoint(
            target_store, target_lane_id, pending, artifact):  # noqa: ANN001
        nonlocal artifact_seen, artifact_bytes
        artifact_seen = Path(artifact)
        artifact_bytes = artifact_seen.read_bytes()
        saved = lanes.load_lanes(target_store)["lanes"][target_lane_id]
        assert saved["publish_pending"]["terminal_rebound"] is True
        gates.set_gate(
            root, name="ci", status="red", severity="blocker", scope="release",
            actor="lead", evidence_source="local_command", reason="checkpoint failure",
        )
        return real_checkpoint(target_store, target_lane_id, pending, artifact)

    monkeypatch.setattr(cli, "_lane_checkpoint_publication", turn_gate_red_at_checkpoint)
    assert _run_raw([
        "lane", "deliver", "--id", lane_id, "--from", "dev",
        "--gate-scope", "release",
    ], root) == 3

    saved = lanes.load_lanes(store)["lanes"][lane_id]
    assert saved["status"] == lanes.STATUS_ACTIVE
    assert saved.get("publish_pending") is not False
    assert _committed_artifacts(store, lane_id) == []
    assert artifact_seen is not None and not artifact_seen.exists()
    quarantine = list(
        (store.dir / "lane-deliveries" / ".recovery-quarantine").glob("*.rejected")
    )
    assert len(quarantine) == 1
    assert quarantine[0].read_bytes() == artifact_bytes
    assert _run_raw([
        "close", "open", "--id", "checkpoint-entry-close", "--from", "lead",
        "--scope", "release", "--revision", head,
        "--lane-artifact", str(artifact_seen),
    ], root) == 0
    assert _run_raw([
        "close", "check", "--id", "checkpoint-entry-close",
    ], root) == 3


def test_retry_quarantines_existing_final_before_target_drift_rollback(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    target_ref = _branch(root)
    _git(root, "checkout", "-q", "-b", "retry-final-target-conflict", base)
    conflicting = _commit(root, "src/core/a.py", "base\ntarget conflict\n")
    _git(root, "checkout", "-q", target_ref)
    lane_id = "retry-existing-final"
    lane = _assign_worktree(root, base, lane_id)
    head = _commit(
        Path(lane["worktree_path"]), "src/core/a.py", "base\nlane candidate\n",
    )
    store = Store(root)
    real_checkpoint = cli._lane_checkpoint_publication
    monkeypatch.setattr(
        cli, "_lane_checkpoint_publication",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("checkpoint crash")),
    )
    argv = ["lane", "deliver", "--id", lane_id, "--from", "dev"]
    assert _run_raw(argv, root) == 2
    monkeypatch.setattr(cli, "_lane_checkpoint_publication", real_checkpoint)

    finals = _committed_artifacts(store, lane_id)
    assert len(finals) == 1
    stale_final = finals[0]
    stale_bytes = stale_final.read_bytes()
    pending = lanes.load_lanes(store)["lanes"][lane_id]
    assert isinstance(pending["publish_pending"], dict)
    assert _git_rc(
        root, "update-ref", f"refs/heads/{target_ref}", conflicting, base,
    ).returncode == 0

    assert _run_raw(argv, root) == 3
    saved = lanes.load_lanes(store)["lanes"][lane_id]
    assert saved["status"] == lanes.STATUS_ACTIVE
    assert saved.get("publish_pending") is not False
    assert _committed_artifacts(store, lane_id) == []
    assert not stale_final.exists()
    quarantine = list(
        (store.dir / "lane-deliveries" / ".recovery-quarantine").glob("*.rejected")
    )
    assert len(quarantine) == 1
    assert quarantine[0].read_bytes() == stale_bytes
    assert _run_raw([
        "close", "open", "--id", "retry-existing-final-close", "--from", "lead",
        "--scope", "release", "--revision", head,
        "--lane-artifact", str(stale_final),
    ], root) == 0
    assert _run_raw([
        "close", "check", "--id", "retry-existing-final-close",
    ], root) == 3


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
                    path_subset=["src/core/legacy-conflict"], base_sha=base,
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

    assert lanes.prefixes_disjoint(
        ["src/core/legacy-conflict"], ["src/core/future"], casefold=False,
    )
    assert _run([
        "lane", "assign", "--id", "rollback-c", "--from", "lead",
        "--assignee", "dev", "--domain", "core", "--base", base,
        "--target", _branch(root), "--path", "src/core/future",
    ], root) == 2
    assert "rollback-c" not in lanes.load_lanes(store)["lanes"]
    assert _run([
        "lane", "assign", "--id", "rollback-a", "--force", "--from", "lead",
        "--assignee", "dev", "--domain", "core", "--base", base,
        "--target", _branch(root), "--path", "src/core",
    ], root) == 2
    assert _run_raw(["lane", "abandon", "--id", "rollback-a"], root) == 2
    still_held = lanes.load_lanes(store)["lanes"]["rollback-a"]
    assert still_held["status"] == lanes.STATUS_DELIVERED
    assert isinstance(still_held["publish_pending"], dict)

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
     ("zero", 0, 2), ("none", None, 2), ("list", [], 2),
     ("empty-dict", {}, 2),
     ("wrong-transaction", {"transaction_id": "e" * 32}, 2),
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


@pytest.mark.parametrize(
    ("marker_case", "marker"),
    [
        ("zero", 0), ("none", None), ("list", []), ("empty-dict", {}),
        ("wrong-transaction", {"transaction_id": "e" * 32}),
    ],
)
def test_reset_refuses_every_non_false_publication_marker(
        tmp_path: Path, marker_case: str, marker: object) -> None:
    root, base = _repo(tmp_path)
    lane_id = f"reset-marker-{marker_case}"
    head = _advisory_delivery_fixture(root, base, lane_id)
    store = Store(root)
    data = lanes.load_lanes(store)
    lane = data["lanes"][lane_id]
    lane["status"] = lanes.STATUS_DELIVERED
    lane["delivered_head"] = head
    lane["delivery_transaction_id"] = "f" * 32
    lane["publish_pending"] = marker
    lanes.save_lanes(store, data)

    assert _run_raw(["reset"], root) == 2
    saved = lanes.load_lanes(store)["lanes"][lane_id]
    assert saved["status"] == lanes.STATUS_DELIVERED
    assert saved["publish_pending"] == marker


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
    assert evals == 6  # initial, prepare/pre-publish, then recovery prepare/pre/post
    final = lanes.load_lanes(store)["lanes"]["prepub"]
    assert final["publish_pending"] is False
    assert len(_committed_artifacts(store, "prepub")) == 1


def test_delivery_retry_after_final_write_failure_freshly_rebinds(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agenttalk import _atomic
    from agenttalk import store as store_mod

    root, base = _repo(tmp_path)
    head = _advisory_delivery_fixture(root, base, "finalwrite")
    store = Store(root)
    real_eval = cli._lane_eval
    evals = 0

    def count_eval(*args, **kwargs):  # noqa: ANN002,ANN003
        nonlocal evals
        evals += 1
        return real_eval(*args, **kwargs)

    failed = False

    def fail_final(real):  # noqa: ANN001, ANN202
        # The record is written by _atomic.write_text before #593 and by the
        # no-replace publisher since; fail its first write either way.
        def write(path, text, **kwargs):  # noqa: ANN001, ANN003, ANN202
            nonlocal failed
            candidate = Path(path)
            if (not failed and candidate.parent == store.dir / "lane-deliveries"
                    and candidate.name.startswith("finalwrite-")):
                failed = True
                raise OSError("injected final-write failure")
            return real(path, text, **kwargs)

        return write

    monkeypatch.setattr(cli, "_lane_eval", count_eval)
    monkeypatch.setattr(_atomic, "write_text", fail_final(_atomic.write_text))
    monkeypatch.setattr(store_mod, "_publish_text_no_replace",
                        fail_final(store_mod._publish_text_no_replace))
    argv = ["lane", "deliver", "--id", "finalwrite", "--from", "dev", "--head", head]
    assert _run_raw(argv, root) == 2
    assert isinstance(lanes.load_lanes(store)["lanes"]["finalwrite"]["publish_pending"], dict)
    assert _committed_artifacts(store, "finalwrite") == []

    assert _run_raw(argv, root) == 0
    assert evals == 6  # initial, prepare/pre-publish, then recovery prepare/pre/post
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
    assert evals == 7  # recovery freshly binds the existing final at prepare/pre/checkpoint
    assert _committed_artifacts(store, "postpub") == finals
    assert lanes.validate_delivery_artifact(
        finals[0], lane_id="postpub", head_sha=head, store=store,
    )["artifact_state"] == lanes.ARTIFACT_COMMITTED


def test_delivery_retry_recovers_when_publication_checkpoint_commits_then_fails(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    lane_id = "checkpoint-ambiguous"
    head = _advisory_delivery_fixture(root, base, lane_id)
    store = Store(root)
    real_checkpoint = cli._lane_checkpoint_publication
    failed = False

    def checkpoint_then_fail(*args, **kwargs):  # noqa: ANN002,ANN003
        nonlocal failed
        real_checkpoint(*args, **kwargs)
        if not failed:
            failed = True
            raise OSError("injected failure after publication checkpoint commit")

    monkeypatch.setattr(cli, "_lane_checkpoint_publication", checkpoint_then_fail)
    argv = ["lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head]
    assert _run_raw(argv, root) == 2
    first_final = _committed_artifacts(store, lane_id)
    assert len(first_final) == 1
    checkpointed = lanes.load_lanes(store)["lanes"][lane_id]
    assert checkpointed["publish_pending"] is False
    assert Path(checkpointed["delivery_artifact"]) == first_final[0]

    monkeypatch.setattr(cli, "_lane_checkpoint_publication", real_checkpoint)
    assert _run_raw(argv, root) == 0
    completed = lanes.load_lanes(store)["lanes"][lane_id]
    assert completed["publish_pending"] is False
    assert _committed_artifacts(store, lane_id) == first_final


def test_publication_checkpoint_rejects_stale_terminal_rebind_nonce(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    lane_id = "checkpoint-stale-rebind"
    head = _advisory_delivery_fixture(root, base, lane_id)
    argv = [
        "lane", "deliver", "--id", lane_id, "--from", "dev", "--head", head,
    ]
    store, lane_state = _leave_rebound_publish_pending(
        root, argv, lane_id, monkeypatch,
    )
    pending = dict(lane_state["publish_pending"])
    artifact = Path(lanes.publish_delivery_artifact(store, pending))
    replacement_nonce = (
        "f" * 32 if pending["terminal_rebind_nonce"] != "f" * 32 else "e" * 32
    )
    data = lanes.load_lanes(store)
    current_pending = dict(data["lanes"][lane_id]["publish_pending"])
    current_pending["terminal_rebind_nonce"] = replacement_nonce
    data["lanes"][lane_id]["publish_pending"] = current_pending
    lanes.save_lanes(store, data)

    with pytest.raises(lanes.LaneError, match="terminal binding"):
        cli._lane_checkpoint_publication(store, lane_id, pending, artifact)

    saved = lanes.load_lanes(store)["lanes"][lane_id]
    assert saved["publish_pending"]["terminal_rebind_nonce"] == replacement_nonce
    assert saved["publish_pending"] is not False
    with pytest.raises(lanes.LaneError, match="pending"):
        lanes.validate_delivery_artifact(
            artifact, lane_id=lane_id, head_sha=head, store=store,
            require_live_marker=True,
        )


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


def test_historical_v1_v2_close_evidence_is_distinct_from_pending_v3_transaction(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, base = _repo(tmp_path)
    lane_id = "legacy-distinct"
    lane = _assign_worktree(root, base, lane_id)
    head = _commit(
        Path(lane["worktree_path"]), "src/core/a.py", "base\nlegacy evidence\n",
    )
    argv = ["lane", "deliver", "--id", lane_id, "--from", "dev"]
    store, lane_state = _leave_rebound_publish_pending(
        root, argv, lane_id, monkeypatch,
    )
    pending = lane_state["publish_pending"]
    prepared = json.loads(Path(pending["prepared_artifact"]).read_text(encoding="utf-8"))

    def write_legacy(label: str, version: int | None, *,
                     waived: bool = False, malformed: bool = False) -> Path:
        payload = json.loads(json.dumps(prepared))
        for key in (
            "artifact_state", "transaction_id", "lane_instance_id",
            "lane_generation", "release_class",
        ):
            payload.pop(key, None)
        if version is None:
            payload.pop("integrity_version", None)
        else:
            payload["integrity_version"] = version
            payload["evaluation_snapshot"]["evaluation_version"] = \
                lanes.LEGACY_EVALUATION_VERSION
        if waived:
            payload["isolation_status"] = "waived"
            payload["worktree_waived"] = True
            payload["worktree_waiver_reason"] = "legacy waiver"
            payload["worktree_waived_by"] = "lead"
            payload["worktree_waived_at"] = "legacy"
        payload["delivery_id"] = f"{lane_id}-{label}"
        payload["integrity_token"] = lanes.compute_integrity_token(store, payload)
        if malformed:
            payload.pop("integrity_token")
        path = lanes.deliveries_dir(store) / f"{lane_id}-{label}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    cases = [
        ("v1", write_legacy("v1", None), 0),
        ("v2", write_legacy("v2", lanes.LEGACY_INTEGRITY_VERSION), 0),
        ("v2-waived", write_legacy(
            "v2-waived", lanes.LEGACY_INTEGRITY_VERSION, waived=True,
        ), 3),
        ("v1-malformed", write_legacy("v1-malformed", None, malformed=True), 3),
    ]
    for label, artifact, expected_rc in cases:
        close_id = f"legacy-distinct-{label}"
        assert _run_raw([
            "close", "open", "--id", close_id, "--from", "lead",
            "--scope", "release", "--revision", head,
            "--lane-artifact", str(artifact),
        ], root) == 0
        assert _run_raw(["close", "check", "--id", close_id], root) == expected_rc

    still_pending = lanes.load_lanes(store)["lanes"][lane_id]
    assert still_pending["status"] == lanes.STATUS_DELIVERED
    assert isinstance(still_pending["publish_pending"], dict)
    assert still_pending["delivery_transaction_id"] == pending["transaction_id"]
    assert set(_committed_artifacts(store, lane_id)) == {case[1] for case in cases}


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
    assert evals == 4  # initial GO plus prepare/pre/post; cleanup retry adds none
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
    assert evals == 4  # initial GO plus prepare/pre/post; cleanup retry adds none
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
    assert evals == 5  # two initial evaluations; winner performs prepare/pre/post
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
        (cli, "_lane_worktree_launch_use"),
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
