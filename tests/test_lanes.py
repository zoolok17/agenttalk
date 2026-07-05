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

import json
import re
import subprocess
from pathlib import Path

import pytest

from agenttalk import cli, lanes
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
        target_head_at_assign=OTHER, epoch_at_assign=None, registry_hash_at_assign="rh1")
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
        argv = [*argv, "--no-worktree", "--worktree-waiver-reason", "legacy deliver-gate test"]
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
    assert json.loads(arts[0].read_text(encoding="utf-8"))["verdict"] == "GO"


def test_cli_check_out_of_bounds_holds(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    br = _branch(root)
    _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
          "--domain", "core", "--base", base, "--target", br, "--path", "src/core"], root)
    head = _commit(root, "src/other/b.py", "o\nstray\n")   # outside src/core
    assert _run(["lane", "check", "--id", "l1", "--head", head], root) == 3


def test_cli_deliver_aborts_fail_closed_on_concurrent_change(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # reviewer-1 BLOCKER: if the lane changed (concurrent reassign) between the
    # pre-lock eval and the locked clear, deliver must FAIL CLOSED (exit 3, NO
    # artifact, lane left active) - never a false success. Force a fingerprint
    # mismatch by making fingerprint() unique per call (eval call != locked call).
    root, base = _repo(tmp_path)
    br = _branch(root)
    _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
          "--domain", "core", "--base", base, "--target", br, "--path", "src/core"], root)
    head = _commit(root, "src/core/a.py", "base\nchange\n")
    counter = {"n": 0}

    def _unique_fp(_lane):
        counter["n"] += 1
        return (counter["n"],)

    monkeypatch.setattr(lanes, "fingerprint", _unique_fp)
    assert _run(["lane", "deliver", "--id", "l1", "--from", "dev", "--head", head], root) == 3
    # no artifact written, lane still active
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
    assert lanes.fingerprint(a) != lanes.fingerprint(b)
    assert lanes.fingerprint(a) != lanes.fingerprint(c)
    assert lanes.fingerprint(a) == lanes.fingerprint(dict(a))


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
    (Store(root).dir / "state").mkdir(parents=True, exist_ok=True)
    (Store(root).dir / "state" / "lanes.json").write_text("{bad", encoding="utf-8")
    # lane status fails closed (exit 2), but an unrelated command still works
    assert _run(["lane", "status"], root) == 2
    assert _run(["status"], root) == 0


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
    # C5a: if the just-written artifact reads back corrupt/wrong, deliver exits nonzero
    # and the lane stays ACTIVE (no false success on unreadable evidence).
    root, base = _repo(tmp_path)
    br = _branch(root)
    _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
          "--domain", "core", "--base", base, "--target", br, "--path", "src/core"], root)
    head = _commit(root, "src/core/a.py", "base\nchange\n")

    def bad_write(store, *, lane, head_sha, **kw):
        p = lanes.delivery_artifact_path(store, lane["lane_id"], head_sha)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{corrupt artifact", encoding="utf-8")
        return p

    monkeypatch.setattr(lanes, "write_delivery_artifact", bad_write)
    assert _run(["lane", "deliver", "--id", "l1", "--from", "dev", "--head", head], root) == 2
    active = {ln["lane_id"] for ln in lanes.active_lanes(lanes.load_lanes(Store(root)))}
    assert "l1" in active                                       # lane NOT cleared


def test_cli_deliver_valid_but_wrong_artifact_keeps_lane_active(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # C5a reviewer-1 P1: a VALID-JSON but semantically-wrong artifact (verdict HOLD) must
    # NOT clear the lane - the readback is semantic, not just structural.
    root, base = _repo(tmp_path)
    br = _branch(root)
    _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
          "--domain", "core", "--base", base, "--target", br, "--path", "src/core"], root)
    head = _commit(root, "src/core/a.py", "base\nchange\n")

    def hold_write(store, *, lane, head_sha, **kw):
        p = lanes.delivery_artifact_path(store, lane["lane_id"], head_sha)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "schema_version": lanes.SCHEMA_VERSION, "lane_id": lane["lane_id"],
            "delivered_head": head_sha, "verdict": "HOLD",
            "holds": [{"code": "x", "detail": "tampered"}]}), encoding="utf-8")
        return p

    monkeypatch.setattr(lanes, "write_delivery_artifact", hold_write)
    assert _run(["lane", "deliver", "--id", "l1", "--from", "dev", "--head", head], root) == 2
    active = {ln["lane_id"] for ln in lanes.active_lanes(lanes.load_lanes(Store(root)))}
    assert "l1" in active                                       # lane NOT cleared on HOLD evidence
