"""Shared validated active and compacted envelopes with disposable fingerprint facts."""
import copy
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from agenttalk import signing


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _trust(store, cfg):
    required = store.signing_enforced()
    project = store.project_id() if required else None
    key = None
    if required:
        try:
            key = signing.load_key(project)
        except (OSError, ValueError):
            pass  # Exact state parity: an unreadable enforced key rejects every envelope.
    return (_digest(cfg), required, project, key)


def validate_scanned_rows(store, cfg, rows, invalid_count, *, trust=None):
    """Validate ALREADY-SCANNED ``(message, path)`` rows against a FRESH context.

    Split out of ``validated_active`` (#246 F1 correctness fix): the raw scan
    (``store._scan_messages_with_paths``) never consults config, roster, or
    signing trust - it just reads and parses files off disk - so it is safe
    for concurrent callers to COALESCE (share one in-flight scan). Validation
    is the opposite: it is exactly what makes the result depend on config,
    roster, and signing trust, so a caller whose context changed (signing
    enforcement flipped on, a roster/config edit landed) must always validate
    with ITS OWN current context, never share a verdict computed under an
    older, possibly now-wrong, context. Callers that coalesce the scan MUST
    call this separately, per caller, rather than sharing its result too.
    """
    roster = store._known_roster(cfg)
    if not roster:
        raise ValueError("project config roster is empty")
    _, required, project, key = trust if trust is not None else _trust(store, cfg)
    valid, rejects = [], invalid_count
    for message, path in rows:
        try:
            message.validate(roster)
            if required:
                if key is None:
                    raise ValueError("signing key unavailable")
                signing.verify_message(message.to_dict(), key, expected_key_id=project)
        except ValueError:
            rejects += 1
        else:
            valid.append((message, path))
    valid.sort(key=lambda row: row[0].id)
    return valid, rejects


def validated_active(store, cfg, checkpoint=None, *, trust=None, **scan_options):
    """Use the canonical store scanner and the legacy state's full validation gate."""
    rows, invalid = store._scan_messages_with_paths(checkpoint=checkpoint, **scan_options)
    return validate_scanned_rows(store, cfg, rows, len(invalid), trust=trust)


@dataclass(frozen=True)
class Envelope:
    id: str
    fields: dict
    digest: str
    source_bytes: int
    partition: str


@dataclass(frozen=True)
class Snapshot:
    generation: int
    started: float
    config_digest: str
    active: tuple
    invalid_count: int
    envelopes: tuple
    discovered_files: int
    discovered_bytes: int
    duration: float
    archives: tuple = ()
    archives_complete: bool = False
    archive_invalid_count: int = 0


def selected_closure(snapshot, selected_ids, *, envelope_limit=50000, byte_limit=128 * 1024 * 1024):
    """Budget the reducer-selected full closure, never the discovery population.

    Selection/linked dependency expansion belongs to the reducer. Missing IDs and
    incomplete discovery cannot produce a successful reduction. No prefix is
    returned on failure; the caller retains its last-known placement.
    """
    found, conflicts = {}, False
    for entry in (*snapshot.envelopes, *snapshot.archives):
        if entry.id in found and found[entry.id].digest != entry.digest:
            conflicts = True
        elif entry.id not in found or entry.partition == "active":
            found[entry.id] = entry
    chosen = [found[mid] for mid in sorted(set(selected_ids)) if mid in found]
    count, size = len(chosen), sum(e.source_bytes for e in chosen)
    status = "complete"
    if not snapshot.archives_complete:
        status = "building"
    elif conflicts:
        status = "conflict"
    elif snapshot.invalid_count or snapshot.archive_invalid_count or len(chosen) != len(set(selected_ids)):
        status = "incomplete"
    if count > envelope_limit or size > byte_limit:
        status = "capacity_exceeded"
    return {"status": status, "selected_envelopes": count, "selected_source_bytes": size,
            "envelope_limit": envelope_limit, "byte_limit": byte_limit,
            "capacity_warning": count * 5 >= envelope_limit * 3 or size * 5 >= byte_limit * 3,
            "items": [dict(copy.deepcopy(e.fields), partition=e.partition) for e in chosen]
            if status == "complete" else []}


class MembershipChanged(ValueError):
    """An ordinary concurrent publication/compaction requires another scan."""


class SnapshotInvalidated(ValueError):
    """A config or trust change asked for a rebuild; the next refresh replaces the snapshot."""


# A published snapshot older than this is "old": a rebuild is overdue or still running. It is
# reported as data (freshness()), never raised, so a reader keeps showing the last good view.
STALE_AFTER_S = 15


def _fresh_error(err):
    """A new exception with the stored one's full diagnosis (type, args, errno, file name): re-raising
    the stored object would add traceback frames (and keep their locals alive) on every read."""
    try:
        fresh = copy.copy(err)
        fresh.__traceback__ = None
        return fresh
    except Exception:  # noqa: BLE001 - an exotic exception type: keep the text, not the object
        return RuntimeError(_failure_text(err))


def _failure_text(err):
    """Never empty: some exceptions (MemoryError(), TimeoutError()) carry no message, and a
    failure must not read as "no failure" just because it has no text."""
    return str(err) or type(err).__name__


class SnapshotService:
    """One worker-owned generation per root; polling reads only the published value."""
    def __init__(self, store, *, clock=time.monotonic, archive_slice_limit=1000):
        self.store, self.clock = store, clock
        self._archive_slice_limit = archive_slice_limit
        self._cache, self._cache_trust = {}, None
        self._archive_error = None
        self.current = None
        self._board = None
        self._board_error = None
        self.error = None
        self._lock = threading.Lock()
        self._busy = False
        self._generation = 0
        self._last_start = float("-inf")
        self._retry_at = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def invalidate(self):
        with self._lock:
            self._generation += 1
            self.error = SnapshotInvalidated("snapshot generation invalidated")
            self._cache_trust = None

    def _membership(self, compacted=False):
        directory = self.store.compacted_dir if compacted else self.store.messages_dir
        if not directory.exists():
            return {}
        result = {}
        for path in directory.iterdir():
            if path.suffix == ".json" or (compacted and ".json." in path.name):
                try:
                    st = path.stat()
                except FileNotFoundError:
                    raise MembershipChanged("snapshot membership changed during scan") from None
                result[path] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        return result

    def _cached_partition(self, cfg, membership, checkpoint, *, compacted=False):
        """Cache invalid verdicts too; archive entries never retain Message/body objects."""
        pending = [p for p, fingerprint in membership.items()
                   if p not in self._cache or self._cache[p][0] != fingerprint or self._cache[p][1] is None]
        # New/changed evidence must advance even if an earlier file stays unreadable.
        pending.sort(key=lambda p: p in self._cache and self._cache[p][0] == membership[p])
        if compacted:
            pending = pending[:self._archive_slice_limit]
        began = self.clock()
        for path in pending:
            rows, _ = validated_active(self.store, cfg, checkpoint, trust=self._cache_trust,
                                       paths=[path], compacted=compacted)
            fact, message = None, None
            if rows:
                message = rows[0][0]
                fields = message.to_dict()
                digest = _digest(fields)
                fields.pop("body", None)
                fact = Envelope(message.id, fields, digest, membership[path][2],
                                "compacted" if compacted else "active")
            self._cache[path] = (membership[path], fact, None if compacted else message)
            if compacted and self.clock() - began >= .25:
                break
        records = [self._cache[p] for p, fingerprint in membership.items()
                   if p in self._cache and self._cache[p][0] == fingerprint]
        return records, len(records) == len(membership)

    def refresh(self):
        with self._lock:
            start = self.clock()
            due = self._retry_at if self._retry_at is not None else self._last_start + 5
            if self._busy or start < due or self._stop.is_set():
                return False
            self._busy, self._last_start = True, start
            self._retry_at = None
            generation = self._generation
        try:
            cfg = self.store.load_config()
            if not self.store._known_roster(cfg):
                raise ValueError("project config roster is empty")
            trust = _trust(self.store, cfg)
            if trust != self._cache_trust:
                self._cache.clear()
                self._cache_trust = trust
            before = self._membership()
            batch, slice_start = 0, self.clock()
            def checkpoint():
                nonlocal batch, slice_start
                if self._stop.is_set():
                    raise OSError("snapshot refresh cancelled")
                batch += 1
                if batch >= 1000 or self.clock() - slice_start >= .25:
                    time.sleep(0)  # yield between bounded reads, never inside HTTP
                    batch, slice_start = 0, self.clock()
            records, _ = self._cached_partition(cfg, before, checkpoint)
            if before != self._membership():
                raise MembershipChanged("snapshot membership changed during scan")
            entries = tuple(r[1] for r in records if r[1] is not None)
            active = tuple(sorted((r[2] for r in records if r[2] is not None), key=lambda m: m.id))
            invalid = len(records) - len(entries)
            archives, archive_before, archive_invalid, complete = (), {}, 0, False
            archive_error = None
            try:
                archive_before = self._membership(compacted=True)
                cold, complete = self._cached_partition(cfg, archive_before, checkpoint, compacted=True)
                archives = tuple(r[1] for r in cold if r[1] is not None)
                archive_invalid = len(cold) - len(archives)
                if archive_before != self._membership(compacted=True):
                    raise MembershipChanged("archive membership changed during scan")
                self._cache = {p: record for p, record in self._cache.items()
                               if p in before or p in archive_before}
            except (OSError, ValueError) as exc:
                archive_error, complete = exc, False
                archives = self.current.archives if self.current else ()
            if before != self._membership():
                raise MembershipChanged("snapshot membership changed during scan")
            if trust != _trust(self.store, self.store.load_config()):
                raise ValueError("snapshot generation changed during scan")
            value = Snapshot(generation + 1, start, trust[0], active, invalid,
                             entries, len(before) + len(archive_before),
                             sum(s[2] for s in (*before.values(), *archive_before.values())),
                             self.clock() - start, archives, complete, archive_invalid)
            # Reduction and gate IO belong to this worker, never a polling handler.
            from agenttalk import gates, work_board_facts, work_board_feed
            board_error = None
            try:
                # Integration facts come from the lead-run verify-merges file: no Git here.
                now = datetime.now(timezone.utc)
                evidence = work_board_facts.load_integration(self.store, cfg, now=now)
                board = work_board_feed.build(value, project=self.store.project_id(),
                                              lead=self.store.sole_lead(), now=now,
                                              gate_state=gates.load_gate_state(self.store.root),
                                              integration=evidence)
                if board["coverage"]["status"] != "complete" and self._board:
                    board["items"] = copy.deepcopy(self._board["items"])
                    board["last_known"] = True
                    board = work_board_feed.bounded(board)
            except Exception as exc:  # board failures must not disable active /api/state
                board, board_error = self._board, type(exc).__name__
            with self._lock:
                if generation != self._generation:
                    return False
                self._generation += 1
                self.current, self.error = value, None
                self._board = board
                self._board_error = board_error
                self._archive_error = archive_error
                if isinstance(archive_error, MembershipChanged):
                    self._retry_at = self.clock() + .25
                    self._wake.set()
            return True
        except Exception as exc:  # errors-as-data, preserving the last published generation
            with self._lock:
                self.error = exc
                if isinstance(exc, MembershipChanged):
                    self._retry_at = self.clock() + .25
                    self._wake.set()
            return False
        finally:
            with self._lock:
                self._busy = False

    def active(self, cfg):
        messages, invalid_count, _freshness = self.active_with_freshness(cfg)
        return messages, invalid_count

    def active_with_freshness(self, cfg):
        """The served messages and their freshness, both read from ONE generation under ONE lock.

        Two separate reads could straddle a refresh and label old messages with the age of the
        newer snapshot (#372).
        """
        with self._lock:
            value = self.current
            if value is None:
                if self.error is not None and not isinstance(self.error, MembershipChanged):
                    raise _fresh_error(self.error)
                raise ValueError("snapshot building")
            if value.config_digest != _digest(cfg):
                raise ValueError("snapshot config generation changed")
            failure = self._real_failure()
            if self.clock() - value.started > STALE_AFTER_S and failure is not None:
                raise _fresh_error(failure)  # old AND the last scan really failed: name the cause
            return (copy.deepcopy(list(value.active)), value.invalid_count,
                    self._freshness_locked(value, failure))

    def _real_failure(self):
        """The last refresh's error unless it was a routine retry or a requested rebuild."""
        err = self.error
        return None if isinstance(err, (MembershipChanged, SnapshotInvalidated)) else err

    def _freshness_locked(self, value, failure):
        age = self.clock() - value.started if value else None
        return {"snapshot_age_s": round(age, 1) if age is not None else None,
                "stale": age is not None and age > STALE_AFTER_S,  # the raw age decides, never the shown one
                "rebuilding": self._busy, "scan_error": _failure_text(failure) if failure is not None else None}

    def freshness(self):
        """How old the served data is, as data: the page decides when that deserves a warning.

        ``scan_error`` is set only for a real failure; a routine retry after a concurrent write
        or a requested rebuild is not one.
        """
        with self._lock:
            return self._freshness_locked(self.current, self._real_failure())

    def coverage(self):
        with self._lock:
            return self._coverage_locked()

    def _coverage_locked(self):
        value = self.current
        status = "building"
        if self.error or self._archive_error or (value and self.clock() - value.started > STALE_AFTER_S):
            status = "stale"
        elif value and value.archives_complete:
            status = "incomplete" if value.invalid_count or value.archive_invalid_count else "complete"
        return {"status": status, "generation": value.generation if value else None,
                "discovered_files": value.discovered_files if value else 0,
                "discovered_bytes": value.discovered_bytes if value else 0,
                "refresh_duration": value.duration if value else 0,
                "cache_size": len(self._cache)}

    def board(self):
        """No source reads/rebuilds: stale placements remain explicitly last-known."""
        from agenttalk.work_board_feed import bounded
        with self._lock:
            feed = copy.deepcopy(self._board)
            board_error = self._board_error
            status = self._coverage_locked()
        if board_error:
            status["status"] = "stale"
        if feed is None:
            return {"schema_version": 1, "items": [], "total_count": None, "omitted_count": None,
                    "target_root_project_id": None, "generated_at": None, "window_days": 7,
                    "legacy": {"open_request_count": None, "known_lower_bound": 0,
                               "counts_by_kind": {}, "examples": [], "truncated": False},
                    "unassigned": {"count": None, "reasons": {}, "examples": []},
                    "truncated": False, "coverage": status, "errors": ["board snapshot building"]}
        feed["coverage"].update({k: v for k, v in status.items() if k not in ("status", "generation")})
        if status["status"] != "complete":
            feed["coverage"]["status"] = status["status"]
            feed.update(last_known=True, total_count=None)
            feed["legacy"]["open_request_count"] = None
        if board_error:
            feed["errors"].append("board projection failed: " + board_error)
        return bounded(feed)

    def start(self):
        def run():
            while not self._stop.is_set():
                with self._lock:
                    delay = max(0, self._retry_at - self.clock()) if self._retry_at is not None else 5
                self._wake.wait(delay)
                self._wake.clear()
                if self._stop.is_set():
                    break
                self.refresh()
        self._thread = threading.Thread(target=run, daemon=True, name="agenttalk-envelope-snapshot")
        self._thread.start()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join()
