"""Shared validated envelopes; archive discovery plugs into Snapshot in B4s."""
import copy
import hashlib
import json
import threading
import time
from dataclasses import dataclass

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


def validated_active(store, cfg, checkpoint=None):
    """Use the canonical store scanner and the legacy state's full validation gate."""
    rows, invalid = store._scan_messages_with_paths(checkpoint=checkpoint)
    roster = store._known_roster(cfg)
    if not roster:
        raise ValueError("project config roster is empty")
    _, required, project, key = _trust(store, cfg)
    valid, rejects = [], len(invalid)
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


def selected_closure(snapshot, selected_ids, *, envelope_limit=50000, byte_limit=128 * 1024 * 1024):
    """Budget the reducer-selected full closure, never the discovery population.

    Selection/linked dependency expansion belongs to B3/B4s. Missing IDs and
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
    elif snapshot.invalid_count or len(chosen) != len(set(selected_ids)):
        status = "incomplete"
    if count > envelope_limit or size > byte_limit:
        status = "capacity_exceeded"
    return {"status": status, "selected_envelopes": count, "selected_source_bytes": size,
            "envelope_limit": envelope_limit, "byte_limit": byte_limit,
            "capacity_warning": count * 5 >= envelope_limit * 3 or size * 5 >= byte_limit * 3,
            "items": [dict(copy.deepcopy(e.fields), partition=e.partition) for e in chosen]
            if status == "complete" else []}


class SnapshotService:
    """One worker-owned generation per root; polling reads only the published value."""
    def __init__(self, store, *, clock=time.monotonic):
        self.store, self.clock = store, clock
        self.current = None
        self.error = None
        self._lock = threading.Lock()
        self._busy = False
        self._generation = 0
        self._last_start = float("-inf")
        self._stop = threading.Event()
        self._thread = None

    def invalidate(self):
        with self._lock:
            self._generation += 1
            self.error = ValueError("snapshot generation invalidated")

    def _membership(self):
        if not self.store.messages_dir.exists():
            return {}
        result = {}
        for path in self.store.messages_dir.iterdir():
            if path.suffix == ".json":
                st = path.stat()
                result[path] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        return result

    def refresh(self):
        with self._lock:
            start = self.clock()
            if self._busy or start - self._last_start < 5 or self._stop.is_set():
                return False
            self._busy, self._last_start = True, start
            generation = self._generation
        try:
            cfg = self.store.load_config()
            trust = _trust(self.store, cfg)
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
            rows, invalid = validated_active(self.store, cfg, checkpoint)
            entries = []
            for message, path in rows:
                fields = message.to_dict()
                digest = _digest(fields)
                fields.pop("body", None)
                entries.append(Envelope(message.id, fields, digest, before[path][2], "active"))
            if before != self._membership() or trust != _trust(self.store, self.store.load_config()):
                raise ValueError("snapshot generation changed during scan")
            value = Snapshot(generation + 1, start, trust[0], tuple(m for m, _ in rows), invalid,
                             tuple(entries), len(before), sum(s[2] for s in before.values()), self.clock() - start)
            with self._lock:
                if generation != self._generation:
                    return False
                self._generation += 1
                self.current, self.error = value, None
            return True
        except Exception as exc:  # errors-as-data, preserving the last published generation
            with self._lock:
                self.error = exc
            return False
        finally:
            with self._lock:
                self._busy = False

    def active(self, cfg):
        with self._lock:
            if self.error:
                raise self.error
            value = self.current
            if value is None:
                raise ValueError("snapshot building")
            if value.config_digest != _digest(cfg):
                raise ValueError("snapshot config generation changed")
            if self.clock() - value.started > 15:
                raise ValueError("snapshot stale")
            return copy.deepcopy(list(value.active)), value.invalid_count

    def coverage(self):
        with self._lock:
            value = self.current
            status = "building"
            if self.error or (value and self.clock() - value.started > 15):
                status = "stale"
            elif value and value.archives_complete:
                status = "complete"
            return {"status": status, "generation": value.generation if value else None,
                    "discovered_files": value.discovered_files if value else 0,
                    "discovered_bytes": value.discovered_bytes if value else 0,
                    "refresh_duration": value.duration if value else 0,
                    "cache_size": len(value.envelopes) + len(value.archives) if value else 0}

    def start(self):
        def run():
            while not self._stop.wait(5):
                self.refresh()
        self._thread = threading.Thread(target=run, daemon=True, name="agenttalk-envelope-snapshot")
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join()
