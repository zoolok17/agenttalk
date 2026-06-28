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


def test_forged_verified_publish_rejected() -> None:
    # reviewer-1 blocker: an open-capture publish must NOT self-declare verified
    # (forging the curation gate). The event-kind <-> authority-state matrix rejects it.
    forged = _publish()
    forged["authority"] = {"state": kn.AUTH_VERIFIED, "resolved_from": "active_agent",
                           "reason": None}
    assert kn.event_problem(forged) is not None
    view = kn.current_view([_publish(note_id="kn-ok"), forged])
    # the forged event is skipped; the legitimate uncurated publish remains current
    assert kn.current_view([forged]) == {}            # forged alone -> nothing valid
    assert view[("cli", "cli.seam")]["id"] == "kn-ok"


def test_incomplete_curate_event_rejected() -> None:
    # reviewer-1 blocker: a curate event missing domain_registry_hash (or other
    # required fields) must be rejected, not accepted-then-evaluated-stale (which
    # would hide the real verified note).
    bad = {"schema_version": 1, "event": "curate", "id": "kn-bad1", "key": "cli.seam",
           "domain_id": "cli", "type": "gotcha", "body": "bad verified line",
           "anchor": {"kind": "path", "path": "src/cli.py"},
           "authority": {"state": "verified"}}        # no domain_registry_hash / curated_by
    assert kn.event_problem(bad) is not None
    # a valid verified note is NOT replaced by the malformed curate
    pub = _publish()
    verify = kn.new_curate_event(base=pub, action="verify", curated_by="o",
                                 resolved_from="curator", at="t2", reason=None)
    rec = kn.resolve_views([pub, verify, bad])[("cli", "cli.seam")]
    assert rec["curated"]["id"] == verify["id"]       # malformed curate skipped


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


# ---- pure: C4b fail-closed anchor staleness (0.40.1)

def test_stale_null_baseline_path_is_hard_stale() -> None:
    # C4b: a path anchor with NO verified_against_sha has no baseline to check change
    # against -> HARD-STALE with a distinct reason (was silently fresh).
    note = _publish(verified_against_sha=None, anchor={"kind": "path", "path": "src/cli.py"})
    v = kn.compute_staleness(note, domain_exists=True, current_registry_hash="rh1",
                             anchor_status=_anchor_status(anchor_exists=True))
    assert kn.STALE_MISSING_BASELINE in v["stale_reasons"]
    assert v["hard_stale"] is True


def test_stale_null_baseline_symbol_is_hard_stale() -> None:
    note = _publish(verified_against_sha=None,
                    anchor={"kind": "symbol", "path": "src/cli.py", "symbol": "main"})
    v = kn.compute_staleness(note, domain_exists=True, current_registry_hash="rh1",
                             anchor_status=_anchor_status(anchor_exists=True))
    assert kn.STALE_MISSING_BASELINE in v["stale_reasons"]
    assert v["hard_stale"] is True


def test_stale_pathless_wp_is_unsupported_hard_stale() -> None:
    # C4b: a pathless wp anchor has no resolver in 0.40.1 -> unsupported/unresolved.
    note = _publish(anchor={"kind": "wp", "mission": "m1", "wp_id": "WP-1"})
    v = kn.compute_staleness(note, domain_exists=True, current_registry_hash="rh1",
                             anchor_status=_anchor_status())
    assert kn.STALE_UNSUPPORTED_WP in v["stale_reasons"]
    assert v["hard_stale"] is True


def test_wp_with_path_uses_path_check_not_unsupported() -> None:
    # a wp anchor WITH a path is path-bound -> normal path staleness, never "unsupported".
    note = _publish(anchor={"kind": "wp", "mission": "m1", "wp_id": "WP-1",
                            "path": "src/cli.py"})
    v = kn.compute_staleness(note, domain_exists=True, current_registry_hash="rh1",
                             anchor_status=_anchor_status(anchor_changed=False))
    assert kn.STALE_UNSUPPORTED_WP not in v["stale_reasons"]
    assert kn.STALE_MISSING_BASELINE not in v["stale_reasons"]   # has a baseline (SHA)


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


# ---- CLI integration: C4a/C4b/C4c/C4d (0.40.1)

def _verify(root: Path, key: str) -> int:
    return _run(["knowledge", "curate", "verify", "--from", "curator", "--domain", "cli",
                 "--key", key], root)


def test_cli_expertise_curated_view_survives_later_uncurated(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    # C4a: a verified note by A, then a later UNCURATED publish for the same key by B,
    # must still credit A (curated view) and NOT B for curated-note authorship.
    root = _repo(tmp_path)
    _run(["knowledge", "publish", "--from", "dev", "--domain", "cli", "--type", "gotcha",
          "--key", "cli.seam", "-m", "A insight", "--anchor-kind", "path",
          "--path", "src/cli.py"], root)
    _verify(root, "cli.seam")                                  # A's note is verified
    _run(["knowledge", "publish", "--from", "lead", "--domain", "cli", "--type", "gotcha",
          "--key", "cli.seam", "-m", "B later proposal", "--anchor-kind", "path",
          "--path", "src/cli.py"], root)                       # B's later uncurated publish
    capsys.readouterr()
    assert _run(["roster", "--expertise", "--json"], root) == 0
    out = json.loads(capsys.readouterr().out)
    by = out["cli"]["curated_notes_by"]
    assert by.get("dev") == 1 and "lead" not in by             # A credited, B not


def test_anchor_status_request_msg_id_exact(tmp_path: Path) -> None:
    # C4b: msg_id is EXACT (no fallback to request_id); scan failure -> unresolvable.
    root = _repo(tmp_path)
    s = Store(root)
    m = s.send(sender="lead", recipient="dev", body="x", meta={"request_id": "R1"})

    def status(anchor):
        return cli._knowledge_anchor_status(s, {"anchor": anchor, "verified_against_sha": None})

    # exact msg_id present + matches -> resolvable
    assert status({"kind": "request", "request_id": "R1", "msg_id": m.id})["target_resolvable"] is True
    # msg_id present but NOT found -> unresolvable even though request_id R1 exists (no fallback)
    assert status({"kind": "request", "request_id": "R1", "msg_id": "nope"})["target_resolvable"] is False
    # msg_id found but request_id mismatches the message's request_id -> unresolvable
    assert status({"kind": "request", "request_id": "OTHER", "msg_id": m.id})["target_resolvable"] is False
    # no msg_id -> resolve by request_id
    assert status({"kind": "request", "request_id": "R1"})["target_resolvable"] is True


def test_anchor_status_request_scan_failure_is_unresolvable(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # C4b: a message scan/read failure must be UNRESOLVABLE, never inferred fresh.
    root = _repo(tmp_path)
    s = Store(root)

    def boom():
        raise RuntimeError("scan failed")

    monkeypatch.setattr(s, "valid_messages", boom)
    st = cli._knowledge_anchor_status(s, {"anchor": {"kind": "request", "request_id": "R1"},
                                          "verified_against_sha": None})
    assert st["target_resolvable"] is False


def test_cli_curate_routes_single_durable_writer(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # C4c: BOTH publish and curate go through kn.write_event_locked (one durable append).
    root = _repo(tmp_path)
    calls = {"n": 0}
    real = kn.write_event_locked

    def counting(store, event):
        calls["n"] += 1
        return real(store, event)

    monkeypatch.setattr(kn, "write_event_locked", counting)
    assert _pub(root) == 0                                     # publish -> writer
    assert _verify(root, "cli.seam") == 0                      # curate -> writer
    assert calls["n"] == 2


def test_cli_torn_tail_cannot_hide_prior_curated(tmp_path: Path,
                                                 capsys: pytest.CaptureFixture) -> None:
    # C4c: a truncated/garbage trailing line never hides the prior valid curated event.
    root = _repo(tmp_path)
    _pub(root)
    _verify(root, "cli.seam")
    with open(kn.notes_path(Store(root)), "a", encoding="utf-8") as fh:
        fh.write('{"event": "curate", truncated...\n')        # torn tail
    capsys.readouterr()
    assert _run(["knowledge", "pull"], root) == 0
    out = capsys.readouterr().out
    assert "1 active" in out and "verified" in out            # curated note still visible


def test_cli_onboard_limit_and_grouping(tmp_path: Path,
                                        capsys: pytest.CaptureFixture) -> None:
    # C4d: onboard is bounded by --limit, deterministic, grouped by domain then type.
    root = _repo(tmp_path)
    for i in range(5):
        _run(["knowledge", "publish", "--from", "dev", "--domain", "cli", "--type",
              "gotcha", "--key", f"k{i}", "-m", "x", "--anchor-kind", "path",
              "--path", "src/cli.py"], root)
        _verify(root, f"k{i}")
    capsys.readouterr()
    assert _run(["knowledge", "onboard", "--limit", "2", "--json"], root) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2                                      # bounded
    # deterministic: same keys both runs
    assert _run(["knowledge", "onboard", "--limit", "2", "--json"], root) == 0
    rows2 = json.loads(capsys.readouterr().out)
    assert [r["key"] for r in rows] == [r["key"] for r in rows2]
    # text shows the truncation note
    assert _run(["knowledge", "onboard", "--limit", "2"], root) == 0
    assert "showing 2 of 5" in capsys.readouterr().out
