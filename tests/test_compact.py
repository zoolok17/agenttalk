"""Tests for WP-B prefix compaction (fix #2): archive a safe prefix of old
messages to the cold archived/compacted/ dir WITHOUT corrupting any live
derivation (epoch / threads / rescind / check) or losing unread, protected, or
invalid messages.

All deterministic — no real sleeping. `now` is pushed an hour ahead where it
matters so the keep_age tail never protects the just-created test messages.

Fix #235: that "an hour ahead" reference must be computed at CALL time, not
once at collection/import time - a slow run (a loaded Windows xdist probe,
or a slow serial run) can put more than an hour between the two, at which
point an import-time snapshot is already in the past and nothing archives.
`_later()` is called fresh by each test that needs it; see
test_compaction_uses_call_time_not_import_time_reference below for the
regression this guards against."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk import threads as th
from agenttalk.store import Store

_BARRIER_META = {"barrier": {"version": 1, "scope": "global", "type": "epoch"}}


def _now_utc() -> datetime:
    """The module's clock source - a single seam so a test can monkeypatch it."""
    return datetime.now(timezone.utc)


def _later() -> datetime:
    """now-ahead so keep_age_days=0 leaves nothing "young" (deterministic).
    Computed at CALL time (fix #235) - never cache this in a module-level
    constant, or a slow run can make it stale before a test actually uses it."""
    return _now_utc() + timedelta(hours=1)


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


# -------------------------------------------------------------- fix #235: clock

def test_compaction_uses_call_time_not_import_time_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix #235 regression: the "an hour ahead" reference must be computed at
    CALL time, not once at collection/import time. A module-level
    `_LATER = ...now() + 1h` captures whatever "now" is at collection; if the
    test body actually runs more than an hour later (a loaded Windows xdist
    probe run, or a slow serial run), that frozen value is already BEHIND the
    freshly-created messages' real timestamps and nothing archives (8
    failures on issue #235's evidence).

    Simulated deterministically, no real sleeps: the module's clock source is
    monkeypatched to pretend "collection" happened 2 real hours before actual
    now, so an import-time-shaped snapshot (`_later()` evaluated under that
    patch) would be `collection_time + 1h` == `real_now - 1h` - one real hour
    BEHIND current time, exactly the shape of the bug. Restoring the real
    clock and computing `_later()` again (the fix, called fresh every time)
    is `real_now + 1h` - correctly ahead - and archives the same store
    correctly. `dry_run=True` on both calls so they compare the same,
    unmodified store."""
    s = _store(tmp_path)
    for i in range(4):
        s.send(sender="alpha", recipient="beta", body=f"old{i}")
    s.send(sender="alpha", recipient="beta", kind="note", body="epoch",
           meta=_BARRIER_META)
    newest = s.send(sender="alpha", recipient="beta", body="newest").id
    s.set_cursor("alpha", newest)
    s.set_cursor("beta", newest)

    real_now = _now_utc()
    monkeypatch.setattr(sys.modules[__name__], "_now_utc",
                        lambda: real_now - timedelta(hours=2))
    stale = _later()   # what an import-time `_LATER` would have frozen forever
    stale_result = cli._run_compaction(s, s.load_config(), keep_count=2,
                                       keep_age_days=0.0, dry_run=True, now=stale)
    assert stale_result["archived"] == [], (
        "bug #235 reproduced: a stale, import-time-shaped reference archives nothing")

    monkeypatch.undo()   # restore the real clock source
    fresh = _later()     # the fix: computed fresh, always ahead of real "now"
    fresh_result = cli._run_compaction(s, s.load_config(), keep_count=2,
                                       keep_age_days=0.0, dry_run=True, now=fresh)
    assert len(fresh_result["archived"]) == 4, "the fix: a fresh reference archives correctly"


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

    later = _later()
    keep_floor, capped_by, comp = cli._compute_keep_floor(
        s, s.load_config(), keep_count=2, keep_age_days=0.0, now=later)
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
        s, s.load_config(), keep_count=1, keep_age_days=0.0, now=_later())
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
        s, s.load_config(), keep_count=2, keep_age_days=0.0, now=_later())
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

    later = _later()
    epoch_before = s.current_epoch()
    threads_before = {a: _thread_state_set(s, a, later) for a in ("alpha", "beta")}
    rescind_before = _run(["wait", "--for", "beta", "--to-request", "r-resc",
                           "--timeout", "1", "--grace", "0", "--quiet"], tmp_path)

    res = cli._run_compaction(s, s.load_config(), keep_count=2,
                              keep_age_days=0.0, dry_run=False, now=later)
    assert len(res["archived"]) == 4, "expected the 4 leading notes archived"

    assert s.current_epoch() == epoch_before
    for a in ("alpha", "beta"):
        assert _thread_state_set(s, a, later) == threads_before[a]
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
    later = _later()
    res = cli._run_compaction(s, s.load_config(), keep_count=2,
                              keep_age_days=0.0, dry_run=False, now=later)
    live = _ids_in_messages_dir(s)
    assert old not in live                      # the old prefix archived
    assert opener in live                        # protected opener kept
    assert {r["id"] for r in res["archived"]} == {old}
    # r1 still derivable as open-outbound for alpha
    assert ("r1", "open-outbound") in _thread_state_set(s, "alpha", later)


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
                              keep_age_days=0.0, dry_run=False, now=_later())
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
                        keep_age_days=0.0, dry_run=False, now=_later())
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
                        keep_age_days=0.0, dry_run=False, now=_later())
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
                        keep_age_days=0.0, dry_run=False, now=_later())
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
    later = _later()
    first = cli._run_compaction(s, s.load_config(), keep_count=2,
                                keep_age_days=0.0, dry_run=False, now=later)
    assert len(first["archived"]) > 0
    second = cli._run_compaction(s, s.load_config(), keep_count=2,
                                 keep_age_days=0.0, dry_run=False, now=later)
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
    later = _later()
    # While r1 is open it pins the floor at the opener -> opener not archivable.
    pre = cli._run_compaction(s, s.load_config(), keep_count=1,
                              keep_age_days=0.0, dry_run=True, now=later)
    assert opener not in {r["id"] for r in pre["archived"]}
    # Both parties close it; now nothing protects it.
    s.close_thread("alpha", "r1", seen_msg_id=opener, reason="done")
    s.close_thread("beta", "r1", seen_msg_id=opener, reason="done")
    post = cli._run_compaction(s, s.load_config(), keep_count=1,
                               keep_age_days=0.0, dry_run=True, now=later)
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
                              keep_age_days=0.0, dry_run=False, now=_later())
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
                              keep_age_days=0.0, dry_run=False, now=_later())
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


# ------------------------------------------- #710: --keep-count 0 must not crash
#
# Zero means "no count-based minimum": the age, unread, epoch, protected-thread and relay protections
# still decide. A negative count is refused (exit 2) before any lock or move. Tests marked (guard) pass
# on the old code too and pin what must not move.


def _tree(store: Store) -> dict[str, bytes]:
    """Every file under the store folder with its bytes: equal before and after = nothing was changed."""
    return {p.relative_to(store.dir).as_posix(): p.read_bytes() for p in sorted(store.dir.rglob("*")) if p.is_file()}


def _read_ahead(store: Store, newest: str) -> str:
    """Both agents have read everything and their cursor is a valid id past the newest message, so the unread
    protection never holds back the newest message (a count of 1 still does). Returns that cursor."""
    ahead = "99991231" + newest[8:]
    for agent in ("alpha", "beta"):
        store.set_cursor(agent, ahead)
    return ahead


def _seed(store: Store, n: int = 5) -> list[str]:
    return [store.send(sender="alpha", recipient="beta", body=f"m{i}").id for i in range(n)]


def _compact_json(root: Path, capsys, *argv: str) -> tuple[int, dict]:
    capsys.readouterr()
    rc = _run(["compact", "--json", *argv], root)
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else {})


@pytest.mark.parametrize("dry", [False, True])
def test_keep_count_zero_on_an_empty_store_finishes(tmp_path: Path, capsys, dry: bool) -> None:
    """(guard) An empty store never crashed, with or without --dry-run."""
    _store(tmp_path)
    rc, out = _compact_json(tmp_path, capsys, "--keep-count", "0", *(["--dry-run"] if dry else []))
    assert rc == 0 and out["archived_count"] == 0


def test_keep_count_zero_real_run_archives_all_but_the_unread_newest(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    ids = _seed(s)
    for agent in ("alpha", "beta"):
        s.set_cursor(agent, ids[-1])  # the newest message is the first unread one
    rc, out = _compact_json(tmp_path, capsys, "--keep-count", "0", "--keep-age-days", "0")
    assert rc == 0
    assert out["archived_ids"] == ids[:-1] and out["capped_by"] == "cursor"
    assert _ids_in_messages_dir(s) == {ids[-1]}
    assert s.read_compact_stamp() is not None


def test_keep_count_zero_dry_run_plans_the_same_and_moves_nothing(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    ids = _seed(s)
    for agent in ("alpha", "beta"):
        s.set_cursor(agent, ids[-1])
    before = _tree(s)
    rc, out = _compact_json(tmp_path, capsys, "--keep-count", "0", "--keep-age-days", "0", "--dry-run")
    assert rc == 0 and out["dry_run"] is True and out["archived_ids"] == ids[:-1]
    assert _tree(s) == before, "a preview changes no file"


def test_keep_count_zero_archives_the_message_a_count_of_one_keeps(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    ids = _seed(s)
    _read_ahead(s, ids[-1])
    _, one = _compact_json(tmp_path, capsys, "--keep-count", "1", "--keep-age-days", "0", "--dry-run")
    rc, zero = _compact_json(tmp_path, capsys, "--keep-count", "0", "--keep-age-days", "0", "--dry-run")
    assert rc == 0
    assert one["archived_ids"] == ids[:-1], "a count of 1 keeps the newest message"
    assert zero["archived_ids"] == ids, "a count of 0 has no count-based minimum"


def test_keep_count_zero_keeps_unread_messages(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    ids = _seed(s)
    s.set_cursor("alpha", ids[-1])
    s.set_cursor("beta", ids[2])  # beta has not read ids[2] onward
    rc, out = _compact_json(tmp_path, capsys, "--keep-count", "0", "--keep-age-days", "0", "--dry-run")
    assert rc == 0 and out["archived_ids"] == ids[:2] and out["capped_by"] == "cursor"


def test_keep_count_zero_keeps_recent_messages(tmp_path: Path, capsys) -> None:
    """The age protection (30 days by default) still holds every message made just now."""
    s = _store(tmp_path)
    ids = _seed(s)
    _read_ahead(s, ids[-1])
    rc, out = _compact_json(tmp_path, capsys, "--keep-count", "0", "--dry-run")
    assert rc == 0 and out["archived_count"] == 0
    assert out["keep_floor"] == ids[0] and "keeptail" in out["capped_by"]


def test_keep_count_zero_keeps_the_epoch_barrier_and_what_follows(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    old = _seed(s, 4)
    barrier = s.send(sender="alpha", recipient="beta", kind="note", body="epoch", meta=_BARRIER_META).id
    newest = s.send(sender="alpha", recipient="beta", body="newest").id
    _read_ahead(s, newest)
    rc, out = _compact_json(tmp_path, capsys, "--keep-count", "0", "--keep-age-days", "0", "--dry-run")
    assert rc == 0 and out["archived_ids"] == old and out["keep_floor"] == barrier
    assert "epoch" in out["capped_by"]


def test_keep_count_zero_keeps_a_protected_thread_group(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    old = s.send(sender="alpha", recipient="beta", body="old").id
    opener = s.send(sender="alpha", recipient="beta", kind="review-request", body="rev", meta={"request_id": "r1"}).id
    tail = [s.send(sender="alpha", recipient="beta", body=str(i)).id for i in range(3)]
    _read_ahead(s, tail[-1])
    rc, out = _compact_json(tmp_path, capsys, "--keep-count", "0", "--keep-age-days", "0")
    assert rc == 0 and out["archived_ids"] == [old]
    assert {opener, *tail} <= _ids_in_messages_dir(s)
    assert ("r1", "open-outbound") in _thread_state_set(s, "alpha", _later())


def test_compute_keep_floor_zero_count_is_no_restriction_not_keep_everything(tmp_path: Path) -> None:
    """None, not "": the keeptail part is skipped, and nothing else is changed by the zero."""
    s = _store(tmp_path)
    ids = _seed(s)
    ahead = _read_ahead(s, ids[-1])
    floor, _, comp = cli._compute_keep_floor(s, s.load_config(), keep_count=0, keep_age_days=0.0, now=_later())
    assert comp["keeptail"] is None and floor == ahead
    _, _, young = cli._compute_keep_floor(s, s.load_config(), keep_count=0, keep_age_days=30.0, now=_now_utc())
    assert young["keeptail"] == ids[0], "the age part still holds the recent messages"


@pytest.mark.parametrize("count", [1, 2, 4])
def test_positive_counts_are_unchanged(tmp_path: Path, capsys, count: int) -> None:
    """(guard) A positive count keeps that many newest messages."""
    s = _store(tmp_path)
    ids = _seed(s)
    _read_ahead(s, ids[-1])
    rc, out = _compact_json(tmp_path, capsys, "--keep-count", str(count), "--keep-age-days", "0", "--dry-run")
    assert rc == 0 and out["archived_ids"] == ids[: len(ids) - count]


def test_a_count_above_the_message_total_keeps_everything(tmp_path: Path, capsys) -> None:
    """(guard) More to keep than exist: the keep-everything fail-safe, as before."""
    s = _store(tmp_path)
    ids = _seed(s)
    _read_ahead(s, ids[-1])
    rc, out = _compact_json(tmp_path, capsys, "--keep-count", "9", "--keep-age-days", "0", "--dry-run")
    assert rc == 0 and out["archived_count"] == 0 and out["keep_floor"] == ""


@pytest.mark.parametrize("dry", [False, True])
@pytest.mark.parametrize("count", ["-1", "-7"])
def test_a_negative_count_from_the_flag_is_refused_and_changes_nothing(
    tmp_path: Path, capsys, count: str, dry: bool
) -> None:
    s = _store(tmp_path)
    ids = _seed(s)
    _read_ahead(s, ids[-1])
    before = _tree(s)
    capsys.readouterr()
    rc = _run(["compact", "--keep-count", count, "--keep-age-days", "0", *(["--dry-run"] if dry else [])], tmp_path)
    err = capsys.readouterr().err
    assert rc == 2
    assert "--keep-count" in err and "0 or more" in err and count in err and "nothing was changed" in err
    assert _tree(s) == before, "no message moved, no stamp, no lock file"


def test_a_negative_count_from_the_configuration_is_refused_and_changes_nothing(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    ids = _seed(s)
    _read_ahead(s, ids[-1])
    _set_compact_config(s, keep_count=-3, keep_age_days=0.0)
    before = _tree(s)
    capsys.readouterr()
    rc = _run(["compact"], tmp_path)
    err = capsys.readouterr().err
    assert rc == 2 and "compact.keep_count" in err and "0 or more" in err and "-3" in err
    assert _tree(s) == before
    # the flag wins over the configuration, so a good flag is not refused for a bad stored value
    rc, out = _compact_json(tmp_path, capsys, "--keep-count", "2", "--dry-run")
    assert rc == 0 and out["archived_count"] == len(ids) - 2


def test_auto_compact_with_a_zero_count_archives_and_with_a_negative_count_keeps_everything(tmp_path: Path) -> None:
    s = _store(tmp_path)
    ids = _seed(s)
    _read_ahead(s, ids[-1])
    _set_compact_config(
        s, enabled=True, keep_count=-1, keep_age_days=0.0, trigger_threshold=1, min_interval_seconds=0.0
    )
    cli._maybe_auto_compact(s, now_epoch=time.time() + 10)
    assert _ids_in_messages_dir(s) == set(ids), "a negative count never reaches the mover"
    _set_compact_config(s, enabled=True, keep_count=0, keep_age_days=0.0, trigger_threshold=1, min_interval_seconds=0.0)
    cli._maybe_auto_compact(s, now_epoch=time.time() + 20)
    assert _ids_in_messages_dir(s) == set()


# ---------------------------------- #710 round 1: a saved count is judged before any int() conversion
#
# Master converted the saved count with int(), which reads 2.5 as 2, "5" as 5 and True as 1; those keep
# working. A saved value that int() turns into 0 (-0.5, 0.5, False, "0") or cannot read ("abc", None), or
# that is negative, is refused by `compact` (exit 2, nothing changed) and ignored by automatic compaction
# (everything is kept), so it can never become "no count-based minimum" by accident.

_BAD_SAVED_COUNTS = [-0.5, -0.99, 0.5, False, "0", "-1", "abc", None]


@pytest.mark.parametrize("dry", [False, True])
@pytest.mark.parametrize("raw", _BAD_SAVED_COUNTS, ids=repr)
def test_a_bad_saved_count_is_refused_and_changes_nothing(tmp_path: Path, capsys, raw, dry: bool) -> None:
    s = _store(tmp_path)
    ids = _seed(s)
    _read_ahead(s, ids[-1])
    _set_compact_config(s, keep_count=raw, keep_age_days=0.0)
    before = _tree(s)
    capsys.readouterr()
    rc = _run(["compact", *(["--dry-run"] if dry else [])], tmp_path)
    err = capsys.readouterr().err
    assert rc == 2
    assert "compact.keep_count" in err and "0 or more" in err and repr(raw) in err and "nothing was changed" in err
    assert _tree(s) == before, "no message moved, no stamp, no lock file"
    # the flag wins over the saved value, so a good flag is not refused for a bad stored one
    rc, out = _compact_json(tmp_path, capsys, "--keep-count", "2", "--dry-run")
    assert rc == 0 and out["archived_count"] == len(ids) - 2


@pytest.mark.parametrize("raw", _BAD_SAVED_COUNTS, ids=repr)
def test_automatic_compaction_keeps_everything_for_a_bad_saved_count(tmp_path: Path, raw) -> None:
    s = _store(tmp_path)
    ids = _seed(s)
    _read_ahead(s, ids[-1])
    _set_compact_config(
        s, enabled=True, keep_count=raw, keep_age_days=0.0, trigger_threshold=1, min_interval_seconds=0.0
    )
    cli._maybe_auto_compact(s, now_epoch=time.time() + 10)
    assert _ids_in_messages_dir(s) == set(ids)
    assert s.read_compact_stamp() is None


@pytest.mark.parametrize(("raw", "kept"), [(2.5, 2), (4.99, 4), (True, 1), ("3", 3), (0, 0), (0.0, 0)], ids=repr)
def test_a_saved_count_master_accepted_keeps_working(tmp_path: Path, capsys, raw, kept: int) -> None:
    """(guard) Fractions of 1 or more, True and numeric text were read through int() before and still are;
    0 and 0.0 are the plain zero. Manual and automatic compaction agree."""
    s = _store(tmp_path)
    ids = _seed(s)
    _read_ahead(s, ids[-1])
    _set_compact_config(s, keep_count=raw, keep_age_days=0.0)
    rc, out = _compact_json(tmp_path, capsys, "--dry-run")
    assert rc == 0 and out["archived_ids"] == ids[: len(ids) - kept]
    _set_compact_config(
        s, enabled=True, keep_count=raw, keep_age_days=0.0, trigger_threshold=1, min_interval_seconds=0.0
    )
    cli._maybe_auto_compact(s, now_epoch=time.time() + 10)
    assert _ids_in_messages_dir(s) == set(ids[len(ids) - kept :])
