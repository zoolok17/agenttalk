"""Lesson packs: curated lessons that a new team's store starts with.

A pack is a file in ``knowledge_packs/`` (``<name>.jsonl``, one lesson per line; an optional
``<name>.retired.json`` lists slugs a later version took back). Loading writes ordinary
``process`` lessons into the team's knowledge store, so retract, search and ``lessons_used``
citations work as for any lesson. What marks a pack lesson:

  * the key ``pack.<name>:<slug>`` (the ``pack.`` prefix is reserved for new publishes),
  * the author, owner and curator ``agenttalk-pack``, and the tag ``pack-<name>``,
  * the provenance pair that proves the loader wrote an event: the author and ``authority.resolved_from``
    set to ``pack``. No ordinary command can set it, so a team member who is called ``agenttalk-pack``
    cannot fake it (the name is also reserved for new rosters and new publishes),
  * the rank: the team's own lessons always come first, and pack lessons never take one of the
    lesson slots injected into a turn. That rank is by where the CONTENT came from: each event carries the
    origin of the publish that produced it, so a reader gets the origin of the version it displays
    (``knowledge.pack_origin_ids``). A team's verify of an unchanged pack lesson does not make it the
    team's. Ownership on reload and unload is a different question, by who acted LAST (a team
    curation is a touch).

The pack name and version are NOT in the events (the event schema is closed). They live in a
ledger, ``knowledge/packs.json``: per pack its version and, per key, the hash of what was loaded.
Who owns a lesson is read from the store (the provenance pair above), not from the ledger, so a run cut
short before the ledger was written is recovered by the next one. The team always wins: a key the team
retracted, republished, re-verified or superseded with a lesson of its own is left alone on every reload,
upgrade and unload; the loader undoes only its own retraction.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from agenttalk import _atomic, knowledge as kn

PACKS_DIR = Path(__file__).resolve().parent / "knowledge_packs"
LEDGER_FILENAME = "packs.json"
LEDGER_SCHEMA = 1
PACK_AUTHOR = kn.PACK_AUTHOR
PACK_RESOLVED_FROM = kn.PACK_RESOLVED_FROM
DEFAULT_PACKS = ("migration",)
REVIEW_AFTER_DAYS = 365
EXPIRES_AFTER_DAYS = 730

_PACK_NAME_RE = re.compile(r"\A[a-z][a-z0-9-]{0,39}\Z")
_SLUG_RE = re.compile(r"\A[a-z0-9]+(-[a-z0-9]+)*\Z")
_LINE_KEYS = ["key", "scope", "trigger", "body", "applies_to", "evidence", "pack", "pack_version"]
_STATE_LOADED = "loaded"
_STATE_RETRACTED = "retracted"      # retracted by the loader (retired, removed or unloaded)


@dataclass(frozen=True)
class PackLesson:
    slug: str
    key: str                # pack.<name>:<slug>
    scope: str
    trigger: str
    body: str
    tags: tuple[str, ...]
    evidence: str
    source_hash: str


@dataclass(frozen=True)
class Pack:
    name: str
    version: int
    lessons: tuple[PackLesson, ...]
    retired: tuple[str, ...] = ()


@dataclass
class Report:
    pack: str
    version: int
    published: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    retracted: list[str] = field(default_factory=list)
    kept_for_team: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"pack": self.pack, "version": self.version, "published": self.published,
                "updated": self.updated, "retracted": self.retracted,
                "kept_for_team": self.kept_for_team, "unchanged": self.unchanged}

    def changed(self) -> bool:
        return bool(self.published or self.updated or self.retracted)


def pack_key(name: str, slug: str) -> str:
    return f"{kn.PACK_KEY_PREFIX}{name}:{slug}"


def _check_name(name: object) -> str:
    if not isinstance(name, str) or not _PACK_NAME_RE.match(name):
        raise kn.KnowledgeError(f"{name!r} is not a valid pack name")
    return name


def available() -> list[str]:
    """Names of the packs that ship with this install."""
    if not PACKS_DIR.is_dir():
        return []
    return sorted(p.stem for p in PACKS_DIR.glob("*.jsonl") if _PACK_NAME_RE.match(p.stem))


def read_pack(name: str) -> Pack:
    """Read and validate a pack file; any bad line refuses the whole pack."""
    _check_name(name)
    path = PACKS_DIR / f"{name}.jsonl"
    if not path.is_file():
        raise kn.KnowledgeError(f"unknown pack {name!r} (available: {', '.join(available()) or 'none'})")
    lessons: list[PackLesson] = []
    versions: set[int] = set()
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError as exc:
            raise kn.KnowledgeError(f"pack {name} line {number}: invalid json: {exc}") from exc
        if not isinstance(raw, dict) or list(raw) != _LINE_KEYS:
            raise kn.KnowledgeError(f"pack {name} line {number}: keys must be exactly {_LINE_KEYS}")
        slug = raw["key"]
        if not isinstance(slug, str) or not _SLUG_RE.match(slug) or len(slug) > 60:
            raise kn.KnowledgeError(f"pack {name} line {number}: key {slug!r} is not a safe slug")
        if slug in seen:
            raise kn.KnowledgeError(f"pack {name} line {number}: duplicate key {slug!r}")
        seen.add(slug)
        if raw["pack"] != name:
            raise kn.KnowledgeError(f"pack {name} line {number}: pack field is {raw['pack']!r}")
        version = raw["pack_version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise kn.KnowledgeError(f"pack {name} line {number}: pack_version must be a whole number")
        versions.add(version)
        tags = raw["applies_to"]
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise kn.KnowledgeError(f"pack {name} line {number}: applies_to must be a list of tags")
        lessons.append(PackLesson(
            slug=slug, key=pack_key(name, slug), scope=raw["scope"], trigger=raw["trigger"],
            body=raw["body"], tags=tuple(tags), evidence=raw["evidence"],
            # The version is left out, so a new pack version rewrites only the lessons whose text changed.
            source_hash=hashlib.sha256(json.dumps(
                {k: v for k, v in raw.items() if k != "pack_version"}, sort_keys=True).encode("utf-8")).hexdigest()))
    if not lessons:
        raise kn.KnowledgeError(f"pack {name} has no lessons")
    if len(versions) != 1:
        raise kn.KnowledgeError(f"pack {name}: every line must carry the same pack_version, got {sorted(versions)}")
    pack = Pack(name=name, version=versions.pop(), lessons=tuple(lessons), retired=_read_retired(name))
    for lesson in pack.lessons:           # validate each lesson the way the store will
        lesson_for_store(lesson, datetime.now(timezone.utc))
        kn.validate_key(lesson.key)
        kn.validate_body(lesson.body)
    return pack


def _read_retired(name: str) -> tuple[str, ...]:
    path = PACKS_DIR / f"{name}.retired.json"
    if not path.is_file():
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise kn.KnowledgeError(f"pack {name}: {path.name} is not valid json: {exc}") from exc
    if not isinstance(raw, list) or not all(isinstance(s, str) and _SLUG_RE.match(s) for s in raw):
        raise kn.KnowledgeError(f"pack {name}: {path.name} must be a list of lesson slugs")
    return tuple(raw)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def lesson_for_store(lesson: PackLesson, now: datetime) -> dict[str, Any]:
    """The store's lesson object for a pack lesson: evidence becomes evidence_ref, the pack tag is
    added, and the dates are relative to the load."""
    name = lesson.key[len(kn.PACK_KEY_PREFIX):].split(":", 1)[0]
    tags = list(dict.fromkeys([*lesson.tags, f"pack-{name}"]))
    stored = {
        "scope": lesson.scope, "trigger": lesson.trigger, "evidence_ref": lesson.evidence,
        "applies_to": tags, "owner": PACK_AUTHOR,
        "review_after": _iso(now + timedelta(days=REVIEW_AFTER_DAYS)),
        "expires_at": _iso(now + timedelta(days=EXPIRES_AFTER_DAYS)),
    }
    return kn.validate_lesson(stored, default_status=kn.LESSON_STATUS_PROPOSED)


# ---------------------------------------------------------------- the ledger

def ledger_path(store) -> Path:
    return kn.knowledge_dir(store) / LEDGER_FILENAME


def read_ledger(store) -> dict[str, Any]:
    path = ledger_path(store)
    if not path.is_file():
        return {"schema_version": LEDGER_SCHEMA, "packs": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise kn.KnowledgeError(f"{path} is not valid json ({exc}); fix or remove it") from exc
    if not isinstance(data, dict) or not isinstance(data.get("packs"), dict):
        raise kn.KnowledgeError(f"{path} is not a pack ledger; fix or remove it")
    problem = _ledger_problem(data)
    if problem:
        raise kn.KnowledgeError(f"{path} is not a pack ledger ({problem}); fix or remove it")
    return data


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _ledger_problem(data: dict) -> str:
    """Why a ledger with a valid top level cannot be used, or an empty string. Every later command indexes
    entry["version"] and entry["keys"], so a hand-edited or newer ledger must stop here, with a plain error."""
    if data.get("schema_version") != LEDGER_SCHEMA:
        return f"schema_version {data.get('schema_version')!r}, expected {LEDGER_SCHEMA}"
    for name, entry in data["packs"].items():
        if (not isinstance(entry, dict) or not _is_count(entry.get("version"))
                or not isinstance(entry.get("keys"), dict)):
            return f"pack {name!r} needs a whole-number version and a keys object"
        for key, state in entry["keys"].items():
            if not isinstance(state, dict) or not all(isinstance(v, str) for v in state.values()):
                return f"pack {name!r}, key {key!r}: the key state must be an object of text values"
    return ""


def _write_ledger(store, ledger: dict[str, Any]) -> None:
    _atomic.write_text(ledger_path(store), json.dumps(ledger, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- who owns a lesson

def _is_ours(event: dict | None) -> bool:
    """True when the loader wrote this event. The proof is its provenance pair (author and
    ``authority.resolved_from == "pack"``), which no ordinary command can produce: a member of the team
    who is called ``agenttalk-pack`` still publishes as ``active_agent`` and curates as curator, owner or
    lead. The name alone is never enough."""
    return bool(event) and kn.has_loader_marks(event)


def _retracted_by_loader(latest: dict) -> bool:
    return kn.is_retracted(latest) and _is_ours(latest)


def _team_touched(view: dict | None, superseded: set[str], key: str) -> bool:
    """True when the team has done anything to a lesson the loader wrote: replaced its text, retracted it,
    re-verified it (the latest event is no longer the loader's), or superseded it with a lesson of its own.

    Ownership is read from the store itself, not from the ledger, so a load or unload that was cut short
    before the ledger was written is recovered on the next run. The one thing the loader undoes is its
    own retraction (an earlier unload or retirement)."""
    if view is None:
        return False
    if not _is_ours(view.get("latest")):
        return True
    return key in superseded


def _superseded_keys(views: dict[tuple, dict]) -> set[str]:
    curated = [rec["curated"] for rec in views.values()
               if rec.get("curated") is not None
               and rec["curated"].get("type") == kn.TYPE_LESSON
               and kn.is_curated(rec["curated"]) and not kn.is_retracted(rec["curated"])]
    return kn.lesson_superseded_keys(curated)


def _matches(latest: dict, lesson: PackLesson) -> bool:
    """True when the stored lesson has the text of the pack lesson (the dates are the load's, so they
    are not compared)."""
    stored = latest.get("lesson") or {}
    wanted = lesson_for_store(lesson, datetime.now(timezone.utc))
    return (latest.get("body") == lesson.body
            and all(stored.get(f) == wanted[f] for f in ("scope", "trigger", "evidence_ref", "applies_to")))


def _store_keys(views: dict[tuple, dict], name: str) -> set[str]:
    prefix = pack_key(name, "")
    return {key for (domain, key) in views if domain == kn.PROCESS_DOMAIN and key.startswith(prefix)}


RegistryHashes = Callable[[], "tuple[str, str]"]     # () -> (registry_hash, process_definition_hash)


def _publish_events(lesson: PackLesson, *, hashes: tuple[str, str], at: datetime) -> tuple[dict, dict]:
    registry_hash, definition_hash = hashes
    when = _iso(at)
    publish = kn.new_publish_event(
        note_id=kn.new_note_id(), key=lesson.key, type=kn.TYPE_LESSON, domain_id=kn.PROCESS_DOMAIN,
        body=lesson.body, anchor=None, verified_against_sha=None, domain_registry_hash=registry_hash,
        domain_definition_hash=definition_hash, author=PACK_AUTHOR, resolved_from=PACK_RESOLVED_FROM,
        at=when, lesson=lesson_for_store(lesson, at))
    curate = kn.new_curate_event(
        base=publish, action="verify", curated_by=PACK_AUTHOR, resolved_from=PACK_RESOLVED_FROM,
        at=when, reason=None, domain_registry_hash=registry_hash, domain_definition_hash=definition_hash)
    return publish, curate


def _retract_event(latest: dict, *, reason: str, hashes: tuple[str, str], at: datetime) -> dict:
    registry_hash, definition_hash = hashes
    return kn.new_curate_event(
        base=latest, action="retract", curated_by=PACK_AUTHOR, resolved_from=PACK_RESOLVED_FROM,
        at=_iso(at), reason=reason, domain_registry_hash=registry_hash,
        domain_definition_hash=definition_hash)


def _ledger_entry(latest: dict, source_hash: str, state: str) -> dict[str, str]:
    return {"hash": kn.payload_hash(latest), "source": source_hash, "state": state}


# ---------------------------------------------------------------- load and unload

def load(store, name: str, *, registry: RegistryHashes, now: datetime | None = None,
         dry_run: bool = False) -> Report:
    """Load (or upgrade) a pack. Writes nothing when the same version is already loaded.

    The events are written first and the ledger last. A run cut short between the two is recovered by
    the next load, which adopts the lessons it finds already written by the loader (same author, same
    text) instead of writing them again, and completes a lesson whose curate event is missing."""
    pack = read_pack(name)
    at = now or datetime.now(timezone.utc)
    report = Report(pack=name, version=pack.version)
    with store._config_lock():
        hashes = registry()
        views, _problems = kn.resolve_views_with_problems(kn.read_events(store)[0])
        superseded = _superseded_keys(views)
        ledger = read_ledger(store)
        entry = ledger["packs"].get(name) or {"version": 0, "keys": {}}
        if pack.version < entry.get("version", 0):
            raise kn.KnowledgeError(
                f"pack {name} version {pack.version} is older than the loaded version {entry['version']}")
        keys: dict[str, dict] = {k: dict(v) for k, v in entry["keys"].items()}
        writes: list[dict] = []
        wanted = {lesson.key: lesson for lesson in pack.lessons}
        retired = {pack_key(name, slug) for slug in pack.retired}

        # Take back what a later version retired or removed, unless the team touched it.
        known = set(keys) | _store_keys(views, name)
        for key in sorted((known - set(wanted)) | (retired & known)):
            view = views.get((kn.PROCESS_DOMAIN, key))
            if view is None or (key not in keys and not _is_ours(view.get("latest"))):
                continue                                    # nothing written, or a key the team made
            if _team_touched(view, superseded, key):
                report.kept_for_team.append(key)
                continue
            latest = view["latest"]
            if not _retracted_by_loader(latest):
                why = "retired" if key in retired else "removed"
                writes.append(_retract_event(
                    latest, reason=f"{why} in pack {name} version {pack.version}", hashes=hashes, at=at))
                report.retracted.append(key)
            keys[key] = {**keys.get(key, {"hash": kn.payload_hash(latest), "source": ""}),
                         "state": _STATE_RETRACTED}

        for lesson in pack.lessons:
            key = lesson.key
            if key in retired:
                continue
            view = views.get((kn.PROCESS_DOMAIN, key))
            if view is None:
                action = "published"
            elif _team_touched(view, superseded, key):
                report.kept_for_team.append(key)            # always checked first: the team wins
                continue
            else:
                latest = view["latest"]
                if _retracted_by_loader(latest) or not kn.is_curated(latest):
                    action = "published"                    # reopen our own retraction, or finish a cut-short load
                elif _matches(latest, lesson):
                    report.unchanged.append(key)
                    keys[key] = _ledger_entry(latest, lesson.source_hash, _STATE_LOADED)
                    continue
                else:
                    action = "updated"
            publish, curate = _publish_events(lesson, hashes=hashes, at=at)
            writes.extend([publish, curate])
            keys[key] = _ledger_entry(publish, lesson.source_hash, _STATE_LOADED)
            (report.published if action == "published" else report.updated).append(key)

        if dry_run:
            return report
        for event in writes:
            kn.write_event_locked(store, event)
        new_entry = {"version": pack.version, "keys": keys}
        if new_entry != entry:
            ledger["packs"][name] = new_entry
            ledger["schema_version"] = LEDGER_SCHEMA
            _write_ledger(store, ledger)
    return report


def unload(store, name: str, *, registry: RegistryHashes, now: datetime | None = None,
           dry_run: bool = False) -> Report:
    """Retract every lesson of the pack that the team has not touched. Works from the store, so it also
    removes what a cut-short load wrote before the ledger existed."""
    _check_name(name)
    at = now or datetime.now(timezone.utc)
    with store._config_lock():
        hashes = registry()
        ledger = read_ledger(store)
        entry = ledger["packs"].get(name)
        views, _problems = kn.resolve_views_with_problems(kn.read_events(store)[0])
        ours = {key for key in _store_keys(views, name) if _is_ours(views[(kn.PROCESS_DOMAIN, key)].get("latest"))}
        if entry is None and not ours:
            raise kn.KnowledgeError(f"pack {name!r} is not loaded")
        entry = entry or {"version": 0, "keys": {}}
        report = Report(pack=name, version=entry.get("version", 0))
        superseded = _superseded_keys(views)
        keys = {k: dict(v) for k, v in entry["keys"].items()}
        writes: list[dict] = []
        for key in sorted(set(keys) | ours):
            view = views.get((kn.PROCESS_DOMAIN, key))
            if view is None:
                continue
            if _team_touched(view, superseded, key):
                report.kept_for_team.append(key)
                continue
            latest = view["latest"]
            if not _retracted_by_loader(latest):
                writes.append(_retract_event(latest, reason=f"pack {name} unloaded", hashes=hashes, at=at))
                report.retracted.append(key)
            keys[key] = {**keys.get(key, {"hash": kn.payload_hash(latest), "source": ""}),
                         "state": _STATE_RETRACTED}
        if dry_run:
            return report
        for event in writes:
            kn.write_event_locked(store, event)
        new_entry = {"version": entry.get("version", 0), "keys": keys}
        if new_entry != entry:
            ledger["packs"][name] = new_entry
            _write_ledger(store, ledger)
    return report


def describe(store) -> list[dict[str, Any]]:
    """One row per available pack: its version and size, and what this store holds now. The counts are
    folded from the store (what is live, what the team retracted or changed), not read from the ledger."""
    ledger = read_ledger(store)["packs"]
    views, _problems = kn.resolve_views_with_problems(kn.read_events(store)[0])
    superseded = _superseded_keys(views)
    rows = []
    for name in available():
        pack = read_pack(name)
        entry = ledger.get(name)
        live = retracted_by_loader = retracted_by_team = changed_by_team = 0
        for key in _store_keys(views, name):
            latest = views[(kn.PROCESS_DOMAIN, key)].get("latest") or {}
            if _team_touched(views[(kn.PROCESS_DOMAIN, key)], superseded, key):
                if kn.is_retracted(latest) or key in superseded:
                    retracted_by_team += 1
                else:
                    changed_by_team += 1
            elif _retracted_by_loader(latest):
                retracted_by_loader += 1
            elif kn.is_curated(latest):
                live += 1
        rows.append({"name": name, "version": pack.version, "lessons": len(pack.lessons),
                     "loaded_version": entry["version"] if entry else None, "loaded": bool(entry) and live > 0,
                     "live": live,
                     "retracted_by_pack": retracted_by_loader, "retracted_by_team": retracted_by_team,
                     "changed_by_team": changed_by_team})
    return rows
