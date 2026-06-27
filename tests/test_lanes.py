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


def _cls(domains, shared=None):
    return {"domains": domains, "shared_paths": shared or [], "unowned": not domains}


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
    cls = {"shared/x.py": _cls([], shared=[{"glob": "shared/**"}])}
    assert lanes.HOLD_SHARED_MISSING_APPROVAL in _codes(_ev(lane, changed, cls))
    lanes.add_shared_approval(lane, path_or_glob="shared", approved_by="lead",
                              reason="ok", at="t", epoch=None, registry_hash="rh1")
    assert _ev(lane, changed, cls)["verdict"] == lanes.VERDICT_GO


def test_hold_active_lane_overlap() -> None:
    other = _lane(lane_id="l2", path_subset=["src/core/sub"])
    v = _ev(_lane(), _changed("src/core/sub/x.py"), {"src/core/sub/x.py": _cls(["core"])},
            active_lanes=[other])
    assert lanes.HOLD_ACTIVE_LANE_OVERLAP in _codes(v)


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


def _repo(tmp_path: Path) -> tuple[Path, str]:
    """A git repo with .agenttalk gitignored + an initialized store + a core domain."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")
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


def test_cli_assign_overlap_refused(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    br = _branch(root)
    assert _run(["lane", "assign", "--id", "l1", "--from", "lead", "--assignee", "dev",
                 "--domain", "core", "--base", base, "--target", br, "--path", "src/core"], root) == 0
    # overlapping subset on a second lane is refused (fail closed)
    assert _run(["lane", "assign", "--id", "l2", "--from", "lead", "--assignee", "dev2",
                 "--domain", "core", "--base", base, "--target", br, "--path", "src/core/sub"], root) == 2


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
