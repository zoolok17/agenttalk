"""Tests for the knowledge layer MVP.

Pure tests cover validators, latest-by-key folding, anchor-relative staleness (one
per reason/caution), and curation authority. CLI integration drives `main(argv)`
against a real git repo whose `.agenttalk/` is gitignored, exercising the JSONL
store, capture-open/curate-gated authority, anchor-relative staleness through the
git adapter, supersede/retract, and the fail-safe reader.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agenttalk import cli, knowledge as kn
from agenttalk.store import Store

SHA = "a" * 40


def _publish(**over) -> dict:
    base = {
        "note_id": "kn-1", "key": "cli.seam", "type": "seam", "domain_id": "cli",
        "body": "the insight not in the code",
        "anchor": {"kind": "path", "path": "src/cli.py"},
        "verified_against_sha": SHA, "domain_registry_hash": "rh1", "author": "dev",
        "resolved_from": "active_agent", "at": "t1"}
    base.update(over)
    return kn.new_publish_event(**base)


def _anchor_status(**over) -> dict:
    st = {"sha_reachable": True, "head_moved": False, "anchor_changed": False,
          "anchor_exists": True, "evidence_match": None, "target_resolvable": True}
    st.update(over)
    return st


# ---- pure: validators

def test_validators_reject_bad_inputs() -> None:
    for bad in [
        lambda: kn.validate_key("has space"),
        lambda: kn.validate_type("bogus"),
        lambda: kn.validate_body("x" * (kn.BODY_MAX_BYTES + 1)),
        lambda: kn.validate_anchor({"kind": "path"}),       # missing path
        lambda: kn.validate_anchor({"kind": "bogus"}),
        lambda: kn.validate_anchor({"kind": "sha", "sha": "short"}),
    ]:
        with pytest.raises(kn.KnowledgeError):
            bad()


def test_publish_is_always_uncurated() -> None:
    assert _publish()["authority"]["state"] == kn.AUTH_UNCURATED


# ---- pure: latest-by-key

def test_current_view_latest_by_key_and_skips_invalid() -> None:
    e1 = _publish(note_id="kn-1", body="old")
    e2 = _publish(note_id="kn-2", body="new")
    view = kn.current_view([e1, {"garbage": True}, e2])  # invalid skipped
    assert view[("cli", "cli.seam")]["id"] == "kn-2"


def test_shallow_invalid_event_does_not_hide_valid_note() -> None:
    # codex blocker 2: a minimally-shaped-but-invalid latest line (no body/type/
    # valid anchor) must NOT become current and hide the prior valid note.
    valid = _publish(note_id="kn-1")
    minimal_bad = {"schema_version": 1, "event": "publish", "id": "kn-bad",
                   "key": "cli.seam", "domain_id": "cli", "anchor": {},
                   "authority": {"state": "uncurated"}}
    assert kn.event_problem(minimal_bad) is not None      # rejected by full validation
    view = kn.current_view([valid, minimal_bad])
    assert view[("cli", "cli.seam")]["id"] == "kn-1"      # valid note remains current


def test_resolve_views_uncurated_publish_does_not_shadow_verified() -> None:
    # codex blocker 1: an uncurated publish over a verified key must NOT replace the
    # authoritative (curated) note.
    pub = _publish(note_id="kn-1")
    verify = kn.new_curate_event(base=pub, action="verify", curated_by="o",
                                 resolved_from="curator", at="t2", reason=None)
    later_uncurated = _publish(note_id="kn-3", body="a fresh unblessed proposal")
    rec = kn.resolve_views([pub, verify, later_uncurated])[("cli", "cli.seam")]
    assert rec["latest"]["id"] == "kn-3"                  # capture sees the proposal
    assert rec["curated"]["authority"]["state"] == kn.AUTH_VERIFIED  # authoritative unchanged
    assert rec["tombstoned"] is False


def test_unsafe_path_anchors_rejected() -> None:
    # codex finding 3: path-bearing anchors must be safe repo-relative paths.
    for bad in ("../outside.py", "C:/outside.py", "..\\outside.py", "/abs/x.py"):
        with pytest.raises(kn.KnowledgeError):
            kn.validate_anchor({"kind": "path", "path": bad})


def test_retract_is_terminal_until_superseded() -> None:
    pub = _publish()
    ret = kn.new_curate_event(base=pub, action="retract", curated_by="owner",
                              resolved_from="owner", at="t2", reason="obsolete")
    view = kn.current_view([pub, ret])
    assert kn.is_retracted(view[("cli", "cli.seam")])
    # a later publish supersedes the tombstone
    pub2 = _publish(note_id="kn-3", body="back")
    view2 = kn.current_view([pub, ret, pub2])
    assert not kn.is_retracted(view2[("cli", "cli.seam")])


# ---- pure: anchor-relative staleness (one per reason/caution)

def test_stale_head_moved_anchor_unchanged_is_caution_not_stale() -> None:
    pub = kn.new_curate_event(base=_publish(), action="verify", curated_by="o",
                              resolved_from="owner", at="t", reason=None)
    v = kn.compute_staleness(pub, domain_exists=True, current_registry_hash="rh1",
                             anchor_status=_anchor_status(head_moved=True, anchor_changed=False))
    assert v["hard_stale"] is False
    assert kn.CAUTION_SHA_NOT_HEAD in v["caution_flags"]


def test_stale_anchor_changed_is_hard_stale() -> None:
    v = kn.compute_staleness(_publish(), domain_exists=True, current_registry_hash="rh1",
                             anchor_status=_anchor_status(head_moved=True, anchor_changed=True))
    assert v["hard_stale"] and kn.STALE_ANCHOR_CHANGED in v["stale_reasons"]


def test_stale_anchor_change_undetermined_is_hard_stale() -> None:
    # could-not-determine (None) must fail closed to stale, never infer fresh
    v = kn.compute_staleness(_publish(), domain_exists=True, current_registry_hash="rh1",
                             anchor_status=_anchor_status(anchor_changed=None))
    assert v["hard_stale"] and kn.STALE_ANCHOR_CHANGED in v["stale_reasons"]


def test_stale_anchor_gone() -> None:
    v = kn.compute_staleness(_publish(), domain_exists=True, current_registry_hash="rh1",
                             anchor_status=_anchor_status(anchor_exists=False))
    assert kn.STALE_ANCHOR_GONE in v["stale_reasons"]


def test_stale_sha_unreachable() -> None:
    v = kn.compute_staleness(_publish(), domain_exists=True, current_registry_hash="rh1",
                             anchor_status=_anchor_status(sha_reachable=False))
    assert kn.STALE_SHA_UNREACHABLE in v["stale_reasons"]


def test_stale_registry_changed_and_domain_gone() -> None:
    v = kn.compute_staleness(_publish(), domain_exists=False, current_registry_hash="rhX",
                             anchor_status=_anchor_status())
    assert kn.STALE_DOMAIN_GONE in v["stale_reasons"]
    assert kn.STALE_REGISTRY_CHANGED in v["stale_reasons"]


def test_stale_retracted() -> None:
    ret = kn.new_curate_event(base=_publish(), action="retract", curated_by="o",
                              resolved_from="owner", at="t", reason="x")
    v = kn.compute_staleness(ret, domain_exists=True, current_registry_hash="rh1",
                             anchor_status=_anchor_status())
    assert kn.STALE_RETRACTED in v["stale_reasons"]


def test_symbol_weak_evidence_is_caution() -> None:
    note = _publish(anchor={"kind": "symbol", "path": "src/cli.py", "symbol": "main"})
    v = kn.compute_staleness(note, domain_exists=True, current_registry_hash="rh1",
                             anchor_status=_anchor_status(evidence_match=None))
    assert kn.CAUTION_WEAK_SYMBOL in v["caution_flags"]


def test_uncurated_is_caution() -> None:
    v = kn.compute_staleness(_publish(), domain_exists=True, current_registry_hash="rh1",
                             anchor_status=_anchor_status())
    assert kn.CAUTION_UNCURATED in v["caution_flags"]


def test_request_anchor_unresolvable() -> None:
    note = _publish(anchor={"kind": "request", "request_id": "q-1"})
    v = kn.compute_staleness(note, domain_exists=True, current_registry_hash="rh1",
                             anchor_status=_anchor_status(target_resolvable=False))
    assert kn.STALE_TARGET_UNRESOLVABLE in v["stale_reasons"]


# ---- pure: authority

def test_curation_authority() -> None:
    assert kn.resolve_curation_authority("a", owner_agents=[], curator_agents=["a"], is_lead=False) == "curator"
    assert kn.resolve_curation_authority("a", owner_agents=["a"], curator_agents=[], is_lead=False) == "owner"
    assert kn.resolve_curation_authority("a", owner_agents=[], curator_agents=[], is_lead=True) == "lead"
    assert kn.resolve_curation_authority("a", owner_agents=["b"], curator_agents=[], is_lead=False) is None


# ---- CLI integration

def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, encoding="utf-8").stdout


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cli.py").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    s = Store(tmp_path)
    s.init(["lead", "dev", "curator"])
    s.set_role("lead", "lead")
    (s.dir / "domains.json").write_text(json.dumps({
        "schema_version": 1, "domains": {"cli": {
            "title": "CLI", "owners": {"agents": ["lead"]},
            "curators": {"agents": ["curator"]}, "owned_globs": ["src/**"]}},
        "shared_paths": []}), encoding="utf-8")
    return tmp_path


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _pub(root: Path, *, who="dev", key="cli.seam") -> int:
    return _run(["knowledge", "publish", "--from", who, "--domain", "cli", "--type",
                 "gotcha", "--key", key, "-m", "the durable insight",
                 "--anchor-kind", "path", "--path", "src/cli.py"], root)


def test_cli_capture_open_curate_gated(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path)
    assert _pub(root) == 0                          # any active agent captures
    capsys.readouterr()
    assert _run(["knowledge", "pull"], root) == 0   # uncurated hidden by default
    assert "0 active" in capsys.readouterr().out
    assert _run(["knowledge", "pull", "--include-uncurated"], root) == 0
    assert "1 active" in capsys.readouterr().out
    # a non-curator cannot verify
    assert _run(["knowledge", "curate", "verify", "--from", "dev", "--domain", "cli",
                 "--key", "cli.seam"], root) == 2
    # the curator can
    assert _run(["knowledge", "curate", "verify", "--from", "curator", "--domain", "cli",
                 "--key", "cli.seam"], root) == 0
    capsys.readouterr()
    assert _run(["knowledge", "pull"], root) == 0   # now curated -> visible
    assert "1 active" in capsys.readouterr().out


def test_cli_uncurated_publish_does_not_hide_verified(tmp_path: Path,
                                                      capsys: pytest.CaptureFixture) -> None:
    # codex blocker 1, end-to-end: publish -> verify -> later uncurated publish of the
    # same key must NOT make the verified note vanish from default pull.
    root = _repo(tmp_path)
    _pub(root)
    _run(["knowledge", "curate", "verify", "--from", "curator", "--domain", "cli",
          "--key", "cli.seam"], root)
    assert _pub(root) == 0          # dev re-publishes the same key (uncurated proposal)
    capsys.readouterr()
    assert _run(["knowledge", "pull"], root) == 0
    out = capsys.readouterr().out
    assert "1 active" in out and "verified" in out      # verified note still visible
    capsys.readouterr()
    assert _run(["knowledge", "pull", "--include-uncurated"], root) == 0
    assert "uncurated" in capsys.readouterr().out       # the proposal shows only here


def test_cli_anchor_change_makes_note_stale(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path)
    _pub(root)
    _run(["knowledge", "curate", "verify", "--from", "curator", "--domain", "cli",
          "--key", "cli.seam"], root)
    # change the anchored file
    (root / "src" / "cli.py").write_text("base\nchanged\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")
    capsys.readouterr()
    assert _run(["knowledge", "pull"], root) == 0
    assert "0 active" in capsys.readouterr().out          # hard-stale, hidden
    assert _run(["knowledge", "pull", "--include-stale"], root) == 0
    out = capsys.readouterr().out
    assert "anchor_path_changed" in out


def test_cli_retract_hides_note(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path)
    _pub(root)
    _run(["knowledge", "curate", "verify", "--from", "curator", "--domain", "cli",
          "--key", "cli.seam"], root)
    assert _run(["knowledge", "curate", "retract", "--from", "curator", "--domain", "cli",
                 "--key", "cli.seam", "--reason", "obsolete"], root) == 0
    capsys.readouterr()
    assert _run(["knowledge", "pull", "--include-stale", "--include-uncurated"], root) == 0
    assert "0 active" in capsys.readouterr().out          # retracted excluded always


def test_cli_search_and_onboard(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path)
    _pub(root, key="cli.parser.note")
    _run(["knowledge", "curate", "verify", "--from", "curator", "--domain", "cli",
          "--key", "cli.parser.note"], root)
    capsys.readouterr()
    assert _run(["knowledge", "search", "durable", "--include-uncurated"], root) == 0
    assert "1 matching" in capsys.readouterr().out
    assert _run(["knowledge", "onboard"], root) == 0
    assert "domain cli" in capsys.readouterr().out


def test_cli_reset_preserves_notes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _pub(root)
    assert kn.notes_path(Store(root)).exists()
    _run(["reset"], root)
    # notes live under .agenttalk/knowledge/, a sibling of state/ - reset keeps them
    assert kn.notes_path(Store(root)).exists()
    events, _ = kn.read_events(Store(root))
    assert len(events) == 1


def test_cli_malformed_line_is_failsafe(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _pub(root)
    # append a torn/garbage line; the reader must skip it and keep the valid note
    with open(kn.notes_path(Store(root)), "a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
    events, problems = kn.read_events(Store(root))
    assert len(events) == 1 and len(problems) == 1
    assert _run(["knowledge", "pull", "--include-uncurated"], root) == 0   # not bricked
    # an unrelated command still works
    assert _run(["status"], root) == 0
