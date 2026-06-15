"""Tests for WP-B prefix compaction (fix #2): archive a safe prefix of old
messages to the cold archived/compacted/ dir WITHOUT corrupting any live
derivation (epoch / threads / rescind / check) or losing unread, protected, or
invalid messages.

All deterministic — no real sleeping. `now` is pushed an hour ahead where it
matters so the keep_age tail never protects the just-created test messages."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk import threads as th
from agenttalk.store import Store

# now-ahead so keep_age_days=0 leaves nothing "young" (deterministic).
_LATER = datetime.now(timezone.utc) + timedelta(hours=1)
_BARRIER_META = {"barrier": {"version": 1, "scope": "global", "type": "epoch"}}


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    return s


def _thread_state_set(store: Store, agent: str, now: datetime) -> set[tuple[str, str]]:
    rows = th.derive_threads(store.valid_messages(), agent=agent,
                             cursor=store.cursor(agent), now=now,
                             closed_rids=cli._closed_rids(store, agent),
                             retired=set(store.retired_agents()))
    return {(t.request_id, t.state) for t in rows}


def _ids_in_messages_dir(store: Store) -> set[str]:
    return {p.stem for p in store.messages_dir.iterdir() if p.suffix == ".json"}


def _hand_write(store: Store, msg_id: str, **fields) -> str:
    """Write a raw message file (bypassing send) with an explicit LOW id so it
    sorts into the archivable prefix. Used to plant delivery-invalid files."""
    body = {"id": msg_id, "ts": "2020-01-01T00:00:00Z", "from": "alpha",
            "to": "beta", "kind": "message", "subject": "", "body": "", "meta": {}}
    body.update(fields)
    (store.messages_dir / f"{msg_id}.json").write_text(
        json.dumps(body), encoding="utf-8")
    return msg_id


def _invalid_idents(store: Store) -> set[str]:
    return {ident for ident, _ in store.list_invalid_messages()}


# ---------------------------------------------------------- keep_floor math

def test_compute_keep_floor_epoch_and_prefix(tmp_path: Path) -> None:
    """Leading plain notes (no thread, before the barrier) are the archivable
    prefix; the barrier holds the floor so everything from it on stays."""
    s = _store(tmp_path)
    notes = [s.send(sender="alpha", recipient="beta", body=f"old{i}").id
             for i in range(4)]
    barrier = s.send(sender="alpha", recipient="beta", kind="note",
                     body="epoch", meta=_BARRIER_META).id
    s.send(sender="alpha", recipient="beta", kind="review-request",
           body="rev", meta={"request_id": "r-open"})
    newest = s.send(sender="alpha", recipient="beta", body="newest").id
    # both agents have consumed everything → cursor doesn't bind
    s.set_cursor("alpha", newest)
    s.set_cursor("beta", newest)

    keep_floor, capped_by, comp = cli._compute_keep_floor(
        s, s.load_config(), keep_count=2, keep_age_days=0.0, now=_LATER)
    assert keep_floor == barrier
    assert "epoch" in capped_by
    # everything below the barrier (the 4 notes) is archivable; nothing else.
    assert comp["epoch"] == barrier
    assert all(n < keep_floor for n in notes)


def test_compute_keep_floor_failsafe_empty_cursor(tmp_path: Path) -> None:
    """An active agent that never consumed (cursor "") => archive nothing."""
    s = _store(tmp_path)
    for i in range(5):
        s.send(sender="alpha", recipient="beta", body=str(i))
    # beta never set a cursor.
    keep_floor, capped_by, _ = cli._compute_keep_floor(
        s, s.load_config(), keep_count=1, keep_age_days=0.0, now=_LATER)
    assert keep_floor == ""
    assert "cursor" in capped_by


def test_compute_keep_floor_no_epoch_is_no_restriction(tmp_path: Path) -> None:
    """No barrier => epoch dimension imposes nothing; other floors still bind."""
    s = _store(tmp_path)
    ids = [s.send(sender="alpha", recipient="beta", body=str(i)).id
           for i in range(5)]
    s.set_cursor("alpha", ids[-1])
    s.set_cursor("beta", ids[-1])
    _, _, comp = cli._compute_keep_floor(
        s, s.load_config(), keep_count=2, keep_age_days=0.0, now=_LATER)
    assert comp["epoch"] is None


# ------------------------------------------------- the HEADLINE invariance

def test_compaction_preserves_all_derivations(tmp_path: Path) -> None:
    """The hard-blocker guard: epoch, per-agent thread states, and a scoped
    rescind wake must be IDENTICAL after compaction archives a real prefix."""
    s = _store(tmp_path)
    for i in range(4):
        s.send(sender="alpha", recipient="beta", body=f"old{i}")   # archivable
    s.send(sender="alpha", recipient="beta", kind="note", body="epoch",
           meta=_BARRIER_META)
    s.send(sender="alpha", recipient="beta", kind="review-request",
           body="rev", meta={"request_id": "r-open"})              # protected
    s.send(sender="alpha", recipient="beta", kind="question",
           body="fire?", meta={"request_id": "r-resc"})
    newest = s.send(sender="alpha", recipient="beta", kind="rescind",
                    body="HOLD", meta={"request_id": "r-resc"}).id  # superseded
    s.set_cursor("alpha", newest)
    s.set_cursor("beta", newest)

    epoch_before = s.current_epoch()
    threads_before = {a: _thread_state_set(s, a, _LATER) for a in ("alpha", "beta")}
    rescind_before = _run(["wait", "--for", "beta", "--to-request", "r-resc",
                           "--timeout", "1", "--grace", "0", "--quiet"], tmp_path)

    res = cli._run_compaction(s, s.load_config(), keep_count=2,
                              keep_age_days=0.0, dry_run=False, now=_LATER)
    assert len(res["archived"]) == 4, "expected the 4 leading notes archived"

    assert s.current_epoch() == epoch_before
    for a in ("alpha", "beta"):
        assert _thread_state_set(s, a, _LATER) == threads_before[a]
    rescind_after = _run(["wait", "--for", "beta", "--to-request", "r-resc",
                          "--timeout", "1", "--grace", "0", "--quiet"], tmp_path)
    assert rescind_before == rescind_after == 3


# ------------------------------------------------ protected / closed / invalid

def test_protected_thread_group_stays_live(tmp_path: Path) -> None:
    """A protected thread's WHOLE group stays; only the older non-thread
    prefix below it is archived."""
    s = _store(tmp_path)
    old = s.send(sender="alpha", recipient="beta", body="old").id
    opener = s.send(sender="alpha", recipient="beta", kind="review-request",
                    body="rev", meta={"request_id": "r1"}).id
    tail = [s.send(sender="alpha", recipient="beta", body=str(i)).id
            for i in range(3)]
    s.set_cursor("alpha", tail[-1])
    s.set_cursor("beta", tail[-1])
    res = cli._run_compaction(s, s.load_config(), keep_count=2,
                              keep_age_days=0.0, dry_run=False, now=_LATER)
    live = _ids_in_messages_dir(s)
    assert old not in live                      # the old prefix archived
    assert opener in live                        # protected opener kept
    assert {r["id"] for r in res["archived"]} == {old}
    # r1 still derivable as open-outbound for alpha
    assert ("r1", "open-outbound") in _thread_state_set(s, "alpha", _LATER)


def test_roster_invalid_file_stays_visible_after_compaction(tmp_path: Path) -> None:
    """Regression (codex MAJOR): a parse-valid but OFF-ROSTER file (delivery-
    invalid) must NOT be archived — it stays in list_invalid_messages so
    status/doctor/prune still see the tamper. The structural scan alone would
    have moved it."""
    s = _store(tmp_path)
    ghost = _hand_write(s, "20200101-000000-000000-aaaa", **{"from": "ghost"})
    for i in range(3):
        s.send(sender="alpha", recipient="beta", body=f"old{i}")
    s.send(sender="alpha", recipient="beta", kind="note", body="epoch",
           meta=_BARRIER_META)
    newest = s.send(sender="alpha", recipient="beta", body="n").id
    s.set_cursor("alpha", newest)
    s.set_cursor("beta", newest)
    assert ghost in _invalid_idents(s)                      # before
    res = cli._run_compaction(s, s.load_config(), keep_count=2,
                              keep_age_days=0.0, dry_run=False, now=_LATER)
    assert len(res["archived"]) >= 1                         # real notes archived
    assert ghost not in {r["id"] for r in res["archived"]}
    assert (s.messages_dir / f"{ghost}.json").exists()       # NOT moved
    assert ghost in _invalid_idents(s)                       # still reportable


def test_hmac_invalid_file_stays_visible_after_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (codex MAJOR), signing enforced: a missing-signature file is
    delivery-invalid and must NOT be archived."""
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "hmac.key"))
    s = _store(tmp_path)
    assert _run(["hmac-init"], tmp_path) == 0
    assert s.signing_enforced()
    for i in range(3):
        s.send(sender="alpha", recipient="beta", body=f"old{i}")   # auto-signed
    s.send(sender="alpha", recipient="beta", kind="note", body="epoch",
           meta=_BARRIER_META)
    newest = s.send(sender="alpha", recipient="beta", body="n").id
    s.set_cursor("alpha", newest)
    s.set_cursor("beta", newest)
    unsigned = _hand_write(s, "20200101-000000-000000-bbbb")        # no signature
    assert unsigned in _invalid_idents(s)
    cli._run_compaction(s, s.load_config(), keep_count=2,
                        keep_age_days=0.0, dry_run=False, now=_LATER)
    assert (s.messages_dir / f"{unsigned}.json").exists()
    assert unsigned in _invalid_idents(s)


def test_invalid_files_are_never_archived(tmp_path: Path) -> None:
    """A tamper/garbage file below the floor must stay visible to
    status/doctor/prune — never moved to cold storage."""
    s = _store(tmp_path)
    ids = [s.send(sender="alpha", recipient="beta", body=str(i)).id
           for i in range(5)]
    # plant a low-id invalid file so it sorts into the archivable prefix
    bad = s.messages_dir / "00000000-000000-000000-zzzz.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    s.set_cursor("alpha", ids[-1])
    s.set_cursor("beta", ids[-1])
    cli._run_compaction(s, s.load_config(), keep_count=1,
                        keep_age_days=0.0, dry_run=False, now=_LATER)
    assert bad.exists(), "invalid file must not be archived"
    invalid_ids = {ident for ident, _ in s.list_invalid_messages()}
    assert "00000000-000000-000000-zzzz" in invalid_ids


# ------------------------------------------------ cursor delivery / idempotent

def test_cursor_delivery_after_archiving_below_cursor(tmp_path: Path) -> None:
    """A cursor that 'fell off' the live store (points below the archived
    prefix) still delivers the correct tail via the fix #1 since_id floor."""
    s = _store(tmp_path)
    ids = [s.send(sender="alpha", recipient="beta", body=str(i)).id
           for i in range(6)]
    # beta has consumed through ids[2]; alpha consumed everything
    s.set_cursor("beta", ids[2])
    s.set_cursor("alpha", ids[-1])
    # keep_floor will be min cursor = ids[2]; archive ids[0], ids[1]
    cli._run_compaction(s, s.load_config(), keep_count=2,
                        keep_age_days=0.0, dry_run=False, now=_LATER)
    delivered = [m.id for m in s.messages_for("beta", since_id=s.cursor("beta"))]
    assert delivered == ids[3:]                  # exclusive of the cursor, correct tail


def test_idempotent_second_run_archives_nothing(tmp_path: Path) -> None:
    s = _store(tmp_path)
    for i in range(4):
        s.send(sender="alpha", recipient="beta", body=f"old{i}")
    s.send(sender="alpha", recipient="beta", kind="note", body="epoch",
           meta=_BARRIER_META)
    newest = s.send(sender="alpha", recipient="beta", body="newest").id
    s.set_cursor("alpha", newest)
    s.set_cursor("beta", newest)
    first = cli._run_compaction(s, s.load_config(), keep_count=2,
                                keep_age_days=0.0, dry_run=False, now=_LATER)
    assert len(first["archived"]) > 0
    second = cli._run_compaction(s, s.load_config(), keep_count=2,
                                 keep_age_days=0.0, dry_run=False, now=_LATER)
    assert second["archived"] == []


def test_ack_to_request_unpins_compaction(tmp_path: Path) -> None:
    """A thread the agent closed via ack --to-request stops pinning the floor,
    so its (now closed) prefix becomes archivable."""
    s = _store(tmp_path)
    opener = s.send(sender="beta", recipient="alpha", kind="review-request",
                    body="rev", meta={"request_id": "r1"}).id
    tail = [s.send(sender="alpha", recipient="beta", body=str(i)).id
            for i in range(3)]
    s.set_cursor("alpha", tail[-1])
    s.set_cursor("beta", tail[-1])
    # While r1 is open it pins the floor at the opener -> opener not archivable.
    pre = cli._run_compaction(s, s.load_config(), keep_count=1,
                              keep_age_days=0.0, dry_run=True, now=_LATER)
    assert opener not in {r["id"] for r in pre["archived"]}
    # Both parties close it; now nothing protects it.
    s.close_thread("alpha", "r1", seen_msg_id=opener, reason="done")
    s.close_thread("beta", "r1", seen_msg_id=opener, reason="done")
    post = cli._run_compaction(s, s.load_config(), keep_count=1,
                               keep_age_days=0.0, dry_run=True, now=_LATER)
    assert opener in {r["id"] for r in post["archived"]}


# ----------------------------------------------------------- crash / re-run

def test_partial_archive_is_resumable(tmp_path: Path) -> None:
    """Per-file moves mean a crashed run leaves the moved files cold-archived
    and the rest live; re-running completes with no loss or double-archive."""
    s = _store(tmp_path)
    notes = [s.send(sender="alpha", recipient="beta", body=f"old{i}").id
             for i in range(4)]
    s.send(sender="alpha", recipient="beta", kind="note", body="epoch",
           meta=_BARRIER_META)
    newest = s.send(sender="alpha", recipient="beta", body="newest").id
    s.set_cursor("alpha", newest)
    s.set_cursor("beta", newest)
    # Simulate a partial crash: hand-archive the first note only.
    s.compacted_dir.mkdir(parents=True, exist_ok=True)
    first = s.messages_dir / f"{notes[0]}.json"
    (s.compacted_dir / first.name).write_bytes(first.read_bytes())
    first.unlink()
    # Re-run: it archives the remaining 3 notes; total archived (cold) == 4.
    res = cli._run_compaction(s, s.load_config(), keep_count=2,
                              keep_age_days=0.0, dry_run=False, now=_LATER)
    assert len(res["archived"]) == 3
    cold = {p.stem for p in s.compacted_dir.iterdir()}
    for n in notes:
        assert n in cold
        assert not (s.messages_dir / f"{n}.json").exists()


def test_old_closed_thread_check_returns_unknown_after_archive(
    tmp_path: Path
) -> None:
    """Documented retention boundary: once a CLOSED request's messages are
    cold-archived, `check --to-request` on it returns unknown (exit 4) — safe
    fail-closed, not byte-identical history. A still-live request is current."""
    s = _store(tmp_path)
    # a fully resolved (closed) request: opener + terminal response, both read
    s.send(sender="alpha", recipient="beta", kind="review-request",
           body="rev", meta={"request_id": "rc"})
    s.send(sender="beta", recipient="alpha", kind="review-result",
           body="lgtm", meta={"request_id": "rc", "status": "approved"})
    s.send(sender="alpha", recipient="beta", kind="note", body="epoch",
           meta=_BARRIER_META)
    newest = s.send(sender="alpha", recipient="beta", body="newest").id
    s.set_cursor("alpha", newest)   # alpha consumed the result -> rc closed
    s.set_cursor("beta", newest)

    # Before: rc is a known (resolved) thread -> current.
    assert _run(["check", "--for", "alpha", "--to-request", "rc"], tmp_path) == 0
    res = cli._run_compaction(s, s.load_config(), keep_count=2,
                              keep_age_days=0.0, dry_run=False, now=_LATER)
    assert len(res["archived"]) >= 2, "rc's messages should have been archived"
    # After: rc is no longer derivable -> unknown (exit 4), fail-closed.
    assert _run(["check", "--for", "alpha", "--to-request", "rc"], tmp_path) == 4


# -------------------------------------------------------------- CLI surface

def test_compact_cli_dry_run_moves_nothing(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    for i in range(4):
        s.send(sender="alpha", recipient="beta", body=f"old{i}")
    s.send(sender="alpha", recipient="beta", kind="note", body="epoch",
           meta=_BARRIER_META)
    newest = s.send(sender="alpha", recipient="beta", body="n").id
    s.set_cursor("alpha", newest)
    s.set_cursor("beta", newest)
    before = _ids_in_messages_dir(s)
    rc = _run(["compact", "--dry-run", "--keep-count", "2", "--keep-age-days",
               "0", "--json"], tmp_path)
    assert rc == 0
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["archived_count"] == 4
    assert _ids_in_messages_dir(s) == before        # nothing moved


def _set_compact_config(store: Store, **knobs) -> None:
    import json as _json
    cfg = _json.loads(store.config_path.read_text(encoding="utf-8"))
    cfg["compact"] = knobs
    store.config_path.write_text(_json.dumps(cfg), encoding="utf-8")


def test_auto_compact_off_by_default_is_noop(tmp_path: Path) -> None:
    """With no compact config (enabled defaults False), the wait-arm hook does
    nothing even when the store is non-trivial."""
    s = _store(tmp_path)
    for i in range(5):
        s.send(sender="alpha", recipient="beta", body=str(i))
    s.set_cursor("alpha", s.cursor("alpha"))
    before = _ids_in_messages_dir(s)
    cli._maybe_auto_compact(s, now_epoch=1_000_000.0)
    assert _ids_in_messages_dir(s) == before
    assert s.read_compact_stamp() is None


def test_auto_compact_runs_when_enabled_then_throttles(tmp_path: Path) -> None:
    """Enabled + over threshold + not throttled => the hook compacts and stamps;
    an immediate second call is throttled (min_interval) => no further archive."""
    s = _store(tmp_path)
    for i in range(4):
        s.send(sender="alpha", recipient="beta", body=f"old{i}")
    s.send(sender="alpha", recipient="beta", kind="note", body="epoch",
           meta=_BARRIER_META)
    newest = s.send(sender="alpha", recipient="beta", body="n").id
    s.set_cursor("alpha", newest)
    s.set_cursor("beta", newest)
    _set_compact_config(s, enabled=True, keep_count=2, keep_age_days=0.0,
                        trigger_threshold=1, min_interval_seconds=3600.0)

    cli._maybe_auto_compact(s, now_epoch=1_000_000.0)
    stamp = s.read_compact_stamp()
    assert stamp is not None and stamp["archived"] == 4
    archived_once = _ids_in_messages_dir(s)

    # Immediately again: throttled (within min_interval) => store unchanged.
    cli._maybe_auto_compact(s, now_epoch=1_000_001.0)
    assert _ids_in_messages_dir(s) == archived_once
