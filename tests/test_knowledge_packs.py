"""Lesson packs: a curated set of lessons that a new team's store starts with.

Pack lessons are marked (a reserved key prefix, a fixed author, one tag), rank after the team's
own lessons, never fill the lesson slots injected into turns, and never override a team decision:
a lesson the team retracted, replaced or republished is left alone on every reload and upgrade.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenttalk import cli, knowledge as kn, knowledge_pack as kp, lesson_context as lc
from agenttalk.store import Store

REPO = Path(__file__).resolve().parents[1]
PROCESS = kn.PROCESS_DOMAIN
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _store(tmp_path: Path) -> Path:
    store = Store(tmp_path)
    store.init(["lead", "dev"])
    store.set_role("lead", "lead")
    return tmp_path


def _run(root: Path, *argv: str) -> int:
    return cli.main(["--root", str(root), *argv])


def _events(root: Path) -> list[dict]:
    return kn.read_events(Store(root))[0]


def _ledger(root: Path) -> dict:
    return json.loads((Store(root).dir / "knowledge" / "packs.json").read_text(encoding="utf-8"))


def _line(slug: str, *, version: int = 1, body: str = "Do the thing. Why: it matters. Check: look.",
          scope: str = "process", tags: tuple[str, ...] = ("migration",)) -> dict:
    return {"key": slug, "scope": scope, "trigger": f"When {slug} applies", "body": body,
            "applies_to": ["migration", *tags[1:]], "evidence": "a practice run", "pack": "demo",
            "pack_version": version}


@pytest.fixture
def packs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A temporary pack folder with a tiny pack called demo; returns a writer for its versions."""
    folder = tmp_path / "packs"
    folder.mkdir()
    monkeypatch.setattr(kp, "PACKS_DIR", folder)

    def write(lines: list[dict], retired: list[str] | None = None) -> None:
        (folder / "demo.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        retired_file = folder / "demo.retired.json"
        if retired is None:
            retired_file.unlink(missing_ok=True)
        else:
            retired_file.write_text(json.dumps(retired), encoding="utf-8")
    return write


def _load(root: Path, name: str = "demo", *extra: str) -> int:
    return _run(root, "knowledge", "pack", "load", name, *extra)


def _live(root: Path, slug: str) -> dict | None:
    return kn.current_view(_events(root)).get((PROCESS, f"pack.demo:{slug}"))


def _team_retract(root: Path, slug: str) -> None:
    assert _run(root, "knowledge", "curate", "retract", "--from", "lead", "--domain", PROCESS,
                "--key", f"pack.demo:{slug}", "--reason", "does not fit us") == 0


# ---- the real pack and the format

def test_every_line_of_the_real_pack_passes_the_stores_validation() -> None:
    pack = kp.read_pack("migration")
    assert pack.version == 1 and len(pack.lessons) >= 100
    for lesson in pack.lessons:
        assert kn.validate_key(lesson.key) == lesson.key and lesson.key.startswith("pack.migration:")
        event = kn.new_publish_event(
            note_id="kn-x", key=lesson.key, type=kn.TYPE_LESSON, domain_id=PROCESS, body=lesson.body,
            anchor=None, verified_against_sha=None, domain_registry_hash="rh",
            domain_definition_hash=kn.VIRTUAL_PROCESS_DOMAIN_HASH, author=kp.PACK_AUTHOR,
            resolved_from="pack", at="2026-10-16T12:00:00Z", lesson=kp.lesson_for_store(lesson, NOW))
        assert kn.event_problem(event) is None, lesson.key
        assert "pack-migration" in event["lesson"]["applies_to"]


def test_a_pack_file_with_a_bad_line_is_refused(packs) -> None:
    packs([_line("ok-one"), {**_line("ok-two"), "extra": 1}])
    with pytest.raises(kn.KnowledgeError, match="keys"):
        kp.read_pack("demo")
    packs([_line("Bad Slug")])
    with pytest.raises(kn.KnowledgeError, match="slug"):
        kp.read_pack("demo")
    packs([_line("ok-one"), _line("ok-two", version=2)])
    with pytest.raises(kn.KnowledgeError, match="version"):
        kp.read_pack("demo")


# ---- loading

def test_load_writes_marked_accepted_lessons_and_a_ledger(tmp_path: Path, packs, capsys) -> None:
    root = _store(tmp_path)
    packs([_line("alpha"), _line("beta", scope="test")])
    assert _load(root) == 0
    events = _events(root)
    assert [e["event"] for e in events] == ["publish", "curate", "publish", "curate"]
    for event in events:
        assert event["key"].startswith("pack.demo:") and event["domain_id"] == PROCESS
        assert event.get("author") == kp.PACK_AUTHOR
        assert "pack" not in event and "pack_version" not in event["lesson"]
    live = _live(root, "alpha")
    assert live["authority"]["state"] == kn.AUTH_VERIFIED and live["lesson"]["status"] == "accepted"
    assert live["lesson"]["owner"] == kp.PACK_AUTHOR and "pack-demo" in live["lesson"]["applies_to"]
    assert live["lesson"]["evidence_ref"] == "a practice run"
    ledger = _ledger(root)["packs"]["demo"]
    assert ledger["version"] == 1 and set(ledger["keys"]) == {"pack.demo:alpha", "pack.demo:beta"}
    assert ledger["keys"]["pack.demo:alpha"]["hash"] == kn.payload_hash(live)
    capsys.readouterr()
    assert _run(root, "knowledge", "search", "--type", "lesson", "alpha") == 0
    assert "pack.demo:alpha" in capsys.readouterr().out


def test_reloading_the_same_version_writes_nothing(tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha"), _line("beta")])
    assert _load(root) == 0
    notes = Store(root).dir / "knowledge" / "notes.jsonl"
    before, ledger = notes.read_bytes(), (Store(root).dir / "knowledge" / "packs.json").read_bytes()
    assert _load(root) == 0
    assert notes.read_bytes() == before
    assert (Store(root).dir / "knowledge" / "packs.json").read_bytes() == ledger


def test_dry_run_writes_nothing(tmp_path: Path, packs, capsys) -> None:
    root = _store(tmp_path)
    packs([_line("alpha")])
    assert _load(root, "demo", "--dry-run") == 0
    assert "would" in capsys.readouterr().out
    assert not (Store(root).dir / "knowledge" / "notes.jsonl").exists()
    assert not (Store(root).dir / "knowledge" / "packs.json").exists()


def test_an_unknown_pack_is_an_error(tmp_path: Path, packs) -> None:
    packs([_line("alpha")])
    assert _load(_store(tmp_path), "nope") == 2


def test_the_team_cannot_publish_under_the_reserved_prefix(tmp_path: Path) -> None:
    root = _store(tmp_path)
    assert _run(root, "knowledge", "publish", "--from", "dev", "--type", "lesson",
                "--key", "pack.mine:x", "--scope", "process", "--trigger", "t",
                "--evidence-ref", "e", "--review-after", "2099-01-01", "--expires-at", "2100-01-01",
                "-m", "a team lesson") == 2


def test_a_team_key_that_the_ledger_does_not_know_is_never_overwritten(tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha"), _line("beta")])
    hand = kn.new_publish_event(
        note_id="kn-hand", key="pack.demo:alpha", type=kn.TYPE_LESSON, domain_id=PROCESS,
        body="Written by hand. Why: x. Check: y.", anchor=None, verified_against_sha=None,
        domain_registry_hash="rh", domain_definition_hash=kn.VIRTUAL_PROCESS_DOMAIN_HASH,
        author="dev", resolved_from="active_agent", at="2026-10-16T11:00:00Z",
        lesson={"scope": "process", "trigger": "t", "evidence_ref": "e", "owner": "dev",
                "review_after": "2099-01-01", "expires_at": "2100-01-01"})
    kn.append_event(Store(root), hand)
    assert _load(root) == 0
    latest = _live(root, "alpha")
    assert latest["id"] == "kn-hand" and latest["author"] == "dev"
    assert _live(root, "beta") is not None


# ---- the team wins

def test_a_team_retraction_survives_reload_and_upgrade(tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha"), _line("beta")])
    _load(root)
    _team_retract(root, "alpha")
    count = len(_events(root))
    assert _load(root) == 0 and len(_events(root)) == count            # reload
    packs([_line("alpha", version=2, body="Changed. Why: new. Check: new."),
           _line("beta", version=2)])
    assert _load(root) == 0                                              # upgrade
    assert kn.is_retracted(_live(root, "alpha"))
    assert len(_events(root)) == count                                   # nothing was rewritten


def test_a_republished_pack_key_is_not_overwritten_by_an_upgrade_but_an_untouched_one_is(
        tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha"), _line("beta")])
    _load(root)
    # The CLI refuses a team publish under the reserved prefix, so a republish can only come from
    # a hand-written event; the loader still notices that the latest event is not its own (it has
    # no loader provenance), so it leaves the lesson alone.
    hand = kn.new_publish_event(
        note_id="kn-hand", key="pack.demo:alpha", type=kn.TYPE_LESSON, domain_id=PROCESS,
        body="Our version. Why: x. Check: y.", anchor=None, verified_against_sha=None,
        domain_registry_hash="rh", domain_definition_hash=kn.VIRTUAL_PROCESS_DOMAIN_HASH,
        author="dev", resolved_from="active_agent", at="2026-10-16T11:00:00Z",
        lesson={"scope": "process", "trigger": "t", "evidence_ref": "e", "owner": "dev",
                "review_after": "2099-01-01", "expires_at": "2100-01-01"})
    kn.append_event(Store(root), hand)
    packs([_line("alpha", version=2, body="Changed. Why: new. Check: new."),
           _line("beta", version=2, body="Changed. Why: new. Check: new.")])
    _load(root)
    assert _live(root, "alpha")["id"] == "kn-hand"
    assert _live(root, "beta")["body"].startswith("Changed.")


def test_a_team_lesson_that_supersedes_a_pack_lesson_keeps_it_retired(tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha")])
    _load(root)
    pub = kn.new_publish_event(
        note_id="kn-team", key="team.better", type=kn.TYPE_LESSON, domain_id=PROCESS,
        body="Our own rule. Why: x. Check: y.", anchor=None, verified_against_sha=None,
        domain_registry_hash="rh", domain_definition_hash=kn.VIRTUAL_PROCESS_DOMAIN_HASH,
        author="dev", resolved_from="active_agent", at="2026-10-16T11:00:00Z",
        lesson={"scope": "process", "trigger": "t", "evidence_ref": "e", "owner": "dev",
                "review_after": "2099-01-01", "expires_at": "2100-01-01",
                "supersedes": ["pack.demo:alpha"]})
    kn.append_event(Store(root), pub)
    assert _run(root, "knowledge", "curate", "verify", "--from", "lead", "--domain", PROCESS,
                "--key", "team.better") == 0
    packs([_line("alpha", version=2, body="Changed. Why: new. Check: new.")])
    count = len(_events(root))
    _load(root)
    assert len(_events(root)) == count


def test_a_later_version_retracts_retired_and_removed_keys_but_not_team_touched_ones(
        tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line(s) for s in ("keep", "retire-me", "removed", "team-retracted")])
    _load(root)
    _team_retract(root, "team-retracted")
    packs([_line("keep", version=2)], retired=["retire-me", "team-retracted"])
    assert _load(root) == 0
    assert not kn.is_retracted(_live(root, "keep"))
    for slug in ("retire-me", "removed"):
        gone = _live(root, slug)
        assert kn.is_retracted(gone) and "pack" in gone["retract_reason"]
        assert gone["authority"]["resolved_from"] == "pack"
    assert _ledger(root)["packs"]["demo"]["version"] == 2
    assert _live(root, "team-retracted")["curated_by"] == "lead"         # the team's retraction stands


def test_an_older_pack_than_the_loaded_one_is_refused(tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha", version=2)])
    _load(root)
    packs([_line("alpha", version=1)])
    assert _load(root) == 2


def test_unload_retracts_untouched_lessons_and_a_reload_leaves_team_retractions_alone(
        tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha"), _line("beta"), _line("gamma")])
    _load(root)
    _team_retract(root, "gamma")
    assert _run(root, "knowledge", "pack", "unload", "demo") == 0
    assert kn.is_retracted(_live(root, "alpha")) and kn.is_retracted(_live(root, "beta"))
    assert _live(root, "gamma")["curated_by"] == "lead"
    assert _load(root) == 0
    assert not kn.is_retracted(_live(root, "alpha"))
    assert kn.is_retracted(_live(root, "gamma")) and _live(root, "gamma")["curated_by"] == "lead"


def test_list_shows_available_and_loaded_packs(tmp_path: Path, packs, capsys) -> None:
    root = _store(tmp_path)
    packs([_line("alpha")])
    assert _run(root, "knowledge", "pack", "list") == 0
    assert "demo" in capsys.readouterr().out
    _load(root)
    capsys.readouterr()
    assert _run(root, "knowledge", "pack", "list", "--json") == 0
    rows = {row["name"]: row for row in json.loads(capsys.readouterr().out)}
    assert rows["demo"]["loaded_version"] == 1 and rows["demo"]["version"] == 1


# ---- ranking: the team always comes first

def _row(key: str, *, scope: str, updated: str, review_due: bool, author: str):
    loader = author == kp.PACK_AUTHOR              # the loader's events carry its provenance
    origin = kn.PACK_RESOLVED_FROM if loader else "active_agent"
    pub = kn.new_publish_event(
        note_id="kn-" + key.replace(".", "-").replace(":", "-"), key=key, type=kn.TYPE_LESSON,
        domain_id=PROCESS, body="Rule. Why: w. Check: c.", anchor=None, verified_against_sha=None,
        domain_registry_hash="rh", domain_definition_hash=kn.VIRTUAL_PROCESS_DOMAIN_HASH,
        author=author, resolved_from=origin, at=updated,
        lesson={"scope": scope, "trigger": "t", "evidence_ref": "e", "owner": author,
                "review_after": "2026-01-01" if review_due else "2099-01-01",
                "expires_at": "2100-01-01"})
    cur = kn.new_curate_event(base=pub, action="verify", curated_by=author if loader else "lead",
                              resolved_from=origin, at=updated, reason=None)
    return [pub, cur]


@pytest.mark.parametrize("team_scope", sorted(kn.LESSON_SCOPES))
@pytest.mark.parametrize("team_due", [False, True])
@pytest.mark.parametrize("pack_scope", sorted(kn.LESSON_SCOPES))
@pytest.mark.parametrize("pack_due", [False, True])
def test_a_team_lesson_beats_a_pack_lesson_at_limit_one(
        team_scope: str, team_due: bool, pack_scope: str, pack_due: bool) -> None:
    # The team lesson is older and the pack lesson is newest: only the pack tier can decide.
    events = (_row("team.rule", scope=team_scope, updated="2026-01-02T00:00:00Z", review_due=team_due,
                   author="dev")
              + _row("pack.demo:rule", scope=pack_scope, updated="2026-10-01T00:00:00Z",
                     review_due=pack_due, author=kp.PACK_AUTHOR))
    views, problems = kn.resolve_views_with_problems(events)
    assert problems == []
    for context in sorted(kn.LESSON_SCOPES):
        shown = kn.select_knowledge_view(
            views, domains={}, registry_hash="rh", anchor_status_by_id={}, lesson_limit=1,
            context_scope=context, now="2026-10-16T00:00:00Z")["lessons"]
        assert [note["key"] for note, _verdict in shown] == ["team.rule"]


def test_pack_lessons_never_fill_the_injected_lesson_slots() -> None:
    events = (_row("pack.demo:rule", scope="process", updated="2026-10-01T00:00:00Z", review_due=False,
                   author=kp.PACK_AUTHOR)
              + _row("team.rule", scope="process", updated="2026-01-02T00:00:00Z", review_due=False,
                     author="dev"))
    picked = lc.select_lessons(events, context_scope="process", tags=["migration"], limit=5)
    assert [note["key"] for note, _verdict in picked] == ["team.rule"]


def test_with_no_team_lessons_the_slots_stay_empty() -> None:
    events = _row("pack.demo:rule", scope="process", updated="2026-10-01T00:00:00Z", review_due=False,
                  author=kp.PACK_AUTHOR)
    assert lc.select_lessons(events, context_scope="process", tags=["migration"], limit=5) == []


# ---- init and the opt-out

def test_init_loads_the_migration_pack_unless_told_not_to(tmp_path: Path) -> None:
    on, off = tmp_path / "on", tmp_path / "off"
    on.mkdir()
    off.mkdir()
    assert cli.main(["init", "--path", str(on), "--agents", "lead,dev"]) == 0
    assert cli.main(["init", "--path", str(off), "--agents", "lead,dev", "--no-packs"]) == 0
    assert _ledger(on)["packs"]["migration"]["version"] == 1
    assert len([e for e in _events(on) if e["event"] == "curate"]) == len(kp.read_pack("migration").lessons)
    assert _events(off) == []
    assert cli.main(["init", "--path", str(on), "--agents", "lead,dev", "--force"]) == 0   # idempotent
    assert len(_events(on)) == 2 * len(kp.read_pack("migration").lessons)


# ---- reachability and packaging

def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9-]{4,}", text.casefold()))


def test_each_pack_lesson_is_in_the_top_five_for_some_word_of_its_trigger(tmp_path: Path) -> None:
    root = _store(tmp_path)
    assert _load(root, "migration") == 0
    views, _ = kn.resolve_views_with_problems(_events(root))
    notes = [rec["curated"] for rec in views.values()]
    texts = {n["key"]: kn._knowledge_search_text(n) for n in notes}      # noqa: SLF001
    for note in notes:
        lesson = note["lesson"]
        best = min(_words(lesson["trigger"]), key=lambda w: sum(w in t for t in texts.values()))
        shown = kn.select_knowledge_view(
            views, domains={}, registry_hash="rh", anchor_status_by_id={}, lesson_limit=5,
            query=best, type_filter=kn.TYPE_LESSON)["lessons"]
        assert note["key"] in [n["key"] for n, _v in shown], (note["key"], best)


def test_the_pack_is_inside_the_built_wheel(tmp_path: Path) -> None:
    """Build the real wheel and look inside it (skipped where the build tool is not installed; the
    release gate builds the wheel anyway)."""
    pytest.importorskip("hatchling")
    import subprocess
    import sys
    import zipfile
    assert (REPO / "src" / "agenttalk" / "knowledge_packs" / "migration.jsonl").is_file()
    subprocess.run([sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(tmp_path)],
                   cwd=REPO, check=True, capture_output=True)
    wheel = next(tmp_path.glob("*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    assert "agenttalk/knowledge_packs/migration.jsonl" in names
    assert "agenttalk/knowledge_pack.py" in names


def test_a_lesson_is_dated_relative_to_the_load() -> None:
    lesson = kp.read_pack("migration").lessons[0]
    stored = kp.lesson_for_store(lesson, NOW)
    review = datetime.fromisoformat(stored["review_after"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(stored["expires_at"].replace("Z", "+00:00"))
    assert NOW + timedelta(days=100) < review < expires


# ---- fix round 1: what the team decides after an unload, interrupted writes, init, old keys

def _team_event(key: str, *, supersedes: tuple[str, ...] = (), body: str = "Team text. Why: x. Check: y.",
                author: str = "dev") -> dict:
    return kn.new_publish_event(
        note_id=kn.new_note_id(), key=key, type=kn.TYPE_LESSON, domain_id=PROCESS, body=body, anchor=None,
        verified_against_sha=None, domain_registry_hash="rh",
        domain_definition_hash=kn.VIRTUAL_PROCESS_DOMAIN_HASH, author=author,
        resolved_from="active_agent", at="2026-10-16T18:00:00Z",
        lesson={"scope": "process", "trigger": "When we change a dependency", "evidence_ref": "fixture",
                "owner": author, "review_after": "2099-01-01", "expires_at": "2100-01-01",
                "supersedes": list(supersedes)})


def _unload(root: Path, name: str = "demo") -> int:
    return _run(root, "knowledge", "pack", "unload", name)


def test_a_team_republish_after_an_unload_survives_a_later_load(tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha")])
    _load(root)
    _unload(root)
    kn.append_event(Store(root), _team_event("pack.demo:alpha", body="Team replacement. Why: x. Check: y."))
    count = len(_events(root))
    assert _load(root) == 0
    assert _live(root, "alpha")["body"].startswith("Team replacement.") and len(_events(root)) == count


def test_a_team_retraction_after_an_unload_survives_a_later_load(tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha")])
    _load(root)
    _unload(root)
    base = _live(root, "alpha")
    kn.append_event(Store(root), kn.new_curate_event(
        base=base, action="retract", curated_by="lead", resolved_from="lead",
        at="2026-10-16T18:00:00Z", reason="our decision"))
    count = len(_events(root))
    assert _load(root) == 0
    assert _live(root, "alpha")["curated_by"] == "lead" and len(_events(root)) == count


def test_a_team_lesson_that_supersedes_an_unloaded_pack_lesson_survives_a_later_load(
        tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha")])
    _load(root)
    _unload(root)
    kn.append_event(Store(root), _team_event("team.better", supersedes=("pack.demo:alpha",)))
    assert _run(root, "knowledge", "curate", "verify", "--from", "lead", "--domain", PROCESS,
                "--key", "team.better") == 0
    count = len(_events(root))
    assert _load(root) == 0
    assert len(_events(root)) == count


class _Boom(OSError):
    pass


def _fail_ledger_once(monkeypatch: pytest.MonkeyPatch) -> None:
    real = kp._write_ledger          # noqa: SLF001
    state = {"left": 1}

    def flaky(store, ledger):
        if state["left"]:
            state["left"] -= 1
            raise _Boom("disk full")
        real(store, ledger)
    monkeypatch.setattr(kp, "_write_ledger", flaky)


def _registry() -> tuple[str, str]:
    return "rh", kn.VIRTUAL_PROCESS_DOMAIN_HASH


def test_a_load_whose_ledger_write_failed_is_recovered_by_the_next_load(
        tmp_path: Path, packs, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _store(tmp_path)
    packs([_line("alpha"), _line("beta")])
    _fail_ledger_once(monkeypatch)
    with pytest.raises(_Boom):
        kp.load(Store(root), "demo", registry=_registry)
    count = len(_events(root))
    assert count == 4 and not (Store(root).dir / "knowledge" / "packs.json").exists()
    assert _load(root) == 0
    assert len(_events(root)) == count                                  # adopted, not written twice
    assert set(_ledger(root)["packs"]["demo"]["keys"]) == {"pack.demo:alpha", "pack.demo:beta"}
    assert _unload(root) == 0                                           # and unload can remove them
    assert kn.is_retracted(_live(root, "alpha")) and kn.is_retracted(_live(root, "beta"))


def test_an_unload_with_no_ledger_still_removes_what_the_loader_wrote(
        tmp_path: Path, packs, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _store(tmp_path)
    packs([_line("alpha")])
    _fail_ledger_once(monkeypatch)
    with pytest.raises(_Boom):
        kp.load(Store(root), "demo", registry=_registry)
    assert _unload(root) == 0
    assert kn.is_retracted(_live(root, "alpha"))


def test_an_unload_whose_ledger_write_failed_is_recovered_by_the_next_unload_and_load(
        tmp_path: Path, packs, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _store(tmp_path)
    packs([_line("alpha")])
    _load(root)
    _fail_ledger_once(monkeypatch)
    with pytest.raises(_Boom):
        kp.unload(Store(root), "demo", registry=_registry)
    assert kn.is_retracted(_live(root, "alpha"))
    count = len(_events(root))
    assert _unload(root) == 0 and len(_events(root)) == count          # nothing is retracted twice
    assert _load(root) == 0
    assert not kn.is_retracted(_live(root, "alpha"))                    # its own retraction is undone


def test_an_upgrade_whose_ledger_write_failed_is_recovered(
        tmp_path: Path, packs, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _store(tmp_path)
    packs([_line("alpha")])
    _load(root)
    packs([_line("alpha", version=2, body="Changed. Why: new. Check: new.")])
    _fail_ledger_once(monkeypatch)
    with pytest.raises(_Boom):
        kp.load(Store(root), "demo", registry=_registry)
    count = len(_events(root))
    assert _load(root) == 0
    assert len(_events(root)) == count
    assert _ledger(root)["packs"]["demo"]["version"] == 2
    assert _unload(root) == 0 and kn.is_retracted(_live(root, "alpha"))


def test_a_load_stopped_between_publish_and_curate_is_completed_by_the_next_load(
        tmp_path: Path, packs, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _store(tmp_path)
    packs([_line("alpha")])
    real, calls = kn.write_event_locked, {"n": 0}

    def stop_after_publish(store, event):
        calls["n"] += 1
        if calls["n"] == 2:
            raise _Boom("killed")
        real(store, event)
    monkeypatch.setattr(kn, "write_event_locked", stop_after_publish)
    with pytest.raises(_Boom):
        kp.load(Store(root), "demo", registry=_registry)
    monkeypatch.setattr(kn, "write_event_locked", real)
    assert not kn.is_curated(_live(root, "alpha"))
    assert _load(root) == 0
    assert kn.is_curated(_live(root, "alpha")) and _live(root, "alpha")["author"] == kp.PACK_AUTHOR
    assert _unload(root) == 0 and kn.is_retracted(_live(root, "alpha"))


def test_a_plain_init_on_an_existing_store_never_loads_packs_or_undoes_an_opt_out(tmp_path: Path) -> None:
    empty, loaded = tmp_path / "empty", tmp_path / "loaded"
    empty.mkdir()
    loaded.mkdir()
    assert cli.main(["init", "--path", str(empty), "--agents", "lead,dev", "--no-packs"]) == 0
    assert cli.main(["init", "--path", str(empty), "--agents", "lead,dev"]) == 0
    assert _events(empty) == []                                         # the opt-out stands
    assert cli.main(["init", "--path", str(loaded), "--agents", "lead,dev"]) == 0
    assert _run(loaded, "knowledge", "pack", "unload", "migration") == 0
    count = len(_events(loaded))
    assert cli.main(["init", "--path", str(loaded), "--agents", "lead,dev"]) == 0
    assert cli.main(["init", "--path", str(loaded), "--agents", "lead,dev", "--force"]) == 0
    assert len(_events(loaded)) == count                                # the unload stands


def test_an_older_team_lesson_whose_key_starts_with_pack_dot_stays_a_team_lesson(tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    old = _team_event("pack.local-rule")
    kn.append_event(Store(root), old)
    assert _run(root, "knowledge", "curate", "verify", "--from", "lead", "--domain", PROCESS,
                "--key", "pack.local-rule") == 0
    assert not kn.has_loader_marks(old)
    picked = lc.select_lessons(_events(root), context_scope="process", tags=["migration"], limit=5)
    assert [n["key"] for n, _v in picked] == ["pack.local-rule"]        # still injected into turns
    packs([_line("alpha")])
    _load(root)
    views, _ = kn.resolve_views_with_problems(_events(root))
    shown = kn.select_knowledge_view(views, domains={}, registry_hash="rh", anchor_status_by_id={},
                                     lesson_limit=1, now="2026-10-16T00:00:00Z")["lessons"]
    assert [n["key"] for n, _v in shown] == ["pack.local-rule"]         # and still ranks first
    publish_argv = ["knowledge", "publish", "--from", "dev", "--type", "lesson", "--scope", "process",
                    "--trigger", "t", "--evidence-ref", "e", "--review-after", "2099-01-01",
                    "--expires-at", "2100-01-01", "-m"]
    assert _run(root, *publish_argv, "An update to our old rule", "--key", "pack.local-rule") == 0
    assert _run(root, *publish_argv, "A new one", "--key", "pack.brand-new") == 2      # new keys: refused
    assert _run(root, *publish_argv, "Overwrite the pack", "--key", "pack.demo:alpha") == 2


def test_pack_list_counts_come_from_the_store_not_the_ledger(tmp_path: Path, packs, capsys) -> None:
    root = _store(tmp_path)
    packs([_line("alpha"), _line("beta"), _line("gamma")])
    _load(root)
    _team_retract(root, "alpha")
    kn.append_event(Store(root), _team_event("pack.demo:beta"))
    capsys.readouterr()
    assert _run(root, "knowledge", "pack", "list", "--json") == 0
    row = {r["name"]: r for r in json.loads(capsys.readouterr().out)}["demo"]
    assert (row["live"], row["retracted_by_team"], row["changed_by_team"]) == (1, 1, 1)
    assert _run(root, "knowledge", "pack", "list") == 0
    assert "1 live" in capsys.readouterr().out


# ---- fix round 2: provenance that an ordinary actor cannot produce

def _old_store_with_a_member_named_like_the_loader(tmp_path: Path) -> Path:
    """A store from before the name was reserved: its roster has an agent called agenttalk-pack."""
    store = Store(tmp_path)
    store.init(["lead", "dev"])
    config = json.loads(store.config_path.read_text(encoding="utf-8"))
    config["agents"].append(kp.PACK_AUTHOR)
    store.config_path.write_text(json.dumps(config), encoding="utf-8")
    store.set_role("lead", "lead")
    store.set_role(kp.PACK_AUTHOR, "lead")
    return tmp_path


def test_a_retraction_by_a_member_named_like_the_loader_is_still_the_teams(tmp_path: Path, packs) -> None:
    root = _old_store_with_a_member_named_like_the_loader(tmp_path)
    packs([_line("alpha")])
    _load(root)
    assert _run(root, "knowledge", "curate", "retract", "--from", kp.PACK_AUTHOR, "--domain", PROCESS,
                "--key", "pack.demo:alpha", "--reason", "our decision") == 0
    count = len(_events(root))
    assert _load(root) == 0
    assert kn.is_retracted(_live(root, "alpha")) and len(_events(root)) == count


def test_a_lesson_by_a_member_named_like_the_loader_is_not_a_pack_lesson(tmp_path: Path) -> None:
    root = _store(tmp_path)
    ours = _team_event("team.rule", author=kp.PACK_AUTHOR)             # the name, but not the loader's path
    kn.append_event(Store(root), ours)
    assert _run(root, "knowledge", "curate", "verify", "--from", "lead", "--domain", PROCESS,
                "--key", "team.rule") == 0
    note = kn.current_view(_events(root))[(PROCESS, "team.rule")]
    assert not kn.has_loader_marks(note)
    picked = lc.select_lessons(_events(root), context_scope="process", tags=["migration"], limit=5)
    assert [n["key"] for n, _v in picked] == ["team.rule"]


def test_an_older_team_lesson_by_a_member_named_like_the_loader_is_not_adopted_or_unloaded(
        tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha")])
    kn.append_event(Store(root), _team_event("pack.demo:alpha", author=kp.PACK_AUTHOR))
    count = len(_events(root))
    assert _load(root) == 0 and len(_events(root)) == count            # not adopted, not overwritten
    assert _unload(root) == 0 and len(_events(root)) == count          # not retracted
    assert not kn.is_retracted(_live(root, "alpha"))


def test_every_event_the_loader_writes_carries_its_provenance(tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha")])
    _load(root)
    _unload(root)
    events = _events(root)
    assert [e["event"] for e in events] == ["publish", "curate", "retract"]
    for event in events:
        assert event["authority"]["resolved_from"] == kn.PACK_RESOLVED_FROM == "pack"
        assert kn.has_loader_marks(event)


def test_the_loaders_name_is_reserved_for_new_rosters_and_new_publishes(tmp_path: Path) -> None:
    assert kp.PACK_AUTHOR == "agenttalk-pack"
    with pytest.raises(ValueError, match="reserved"):
        Store(tmp_path / "a").init(["lead", kp.PACK_AUTHOR])
    root = _store(tmp_path / "b")
    with pytest.raises(ValueError, match="reserved"):
        Store(root).add_agent(kp.PACK_AUTHOR)
    old = _old_store_with_a_member_named_like_the_loader(tmp_path / "c")
    assert _run(old, "knowledge", "publish", "--from", kp.PACK_AUTHOR, "--type", "lesson", "--scope", "process",
                "--key", "team.rule", "--trigger", "t", "--evidence-ref", "e", "--review-after", "2099-01-01",
                "--expires-at", "2100-01-01", "-m", "Our lesson.") == 2


# ---- fix round 3: rank by where the content came from, not by who acted last

def _verify(root: Path, key: str) -> int:
    return _run(root, "knowledge", "curate", "verify", "--from", "lead", "--domain", PROCESS, "--key", key)


def test_verifying_an_unchanged_pack_lesson_keeps_it_a_pack_lesson(tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    kn.append_event(Store(root), _team_event("team.rule"))
    assert _verify(root, "team.rule") == 0
    packs([_line("alpha")])
    _load(root)
    assert _verify(root, "pack.demo:alpha") == 0                       # the lead re-verifies it, unchanged
    events = _events(root)
    picked = lc.select_lessons(events, context_scope="process", tags=["migration"], limit=5)
    assert [n["key"] for n, _v in picked] == ["team.rule"]            # it still takes no injected slot
    views, _ = kn.resolve_views_with_problems(events)
    shown = kn.select_knowledge_view(views, domains={}, registry_hash="rh", anchor_status_by_id={},
                                     lesson_limit=1)["lessons"]
    assert [n["key"] for n, _v in shown] == ["team.rule"]             # and still ranks after the team's


def test_a_team_republish_of_the_same_key_becomes_a_team_lesson_for_ranking(tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha"), _line("beta")])
    _load(root)
    kn.append_event(Store(root), _team_event("pack.demo:alpha", body="Our version. Why: x. Check: y."))
    assert _verify(root, "pack.demo:alpha") == 0
    events = _events(root)
    picked = lc.select_lessons(events, context_scope="process", tags=["migration"], limit=5)
    assert [n["key"] for n, _v in picked] == ["pack.demo:alpha"]      # its content is now the team's
    views, _ = kn.resolve_views_with_problems(events)
    shown = kn.select_knowledge_view(views, domains={}, registry_hash="rh", anchor_status_by_id={},
                                     lesson_limit=1)["lessons"]
    assert [n["key"] for n, _v in shown] == ["pack.demo:alpha"]


def test_a_lesson_the_team_verified_is_left_alone_by_a_later_upgrade(tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha")])
    _load(root)
    assert _verify(root, "pack.demo:alpha") == 0
    count = len(_events(root))
    packs([_line("alpha", version=2, body="Changed. Why: new. Check: new.")])
    assert _load(root) == 0
    assert len(_events(root)) == count                                 # a curation by the team is a touch


# ---- fix round 4: origin belongs to each publish event; a reader gets the origin of the version it shows

def test_an_uncurated_team_republish_does_not_promote_the_pack_text_that_is_still_shown(
        tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    kn.append_event(Store(root), _team_event("team.rule"))
    assert _verify(root, "team.rule") == 0
    packs([_line("alpha")])
    _load(root)
    # A team proposal for the same key, not yet verified: readers still show the verified PACK text.
    kn.append_event(Store(root), _team_event("pack.demo:alpha", body="Our proposal. Why: x. Check: y."))
    events = _events(root)
    picked = lc.select_lessons(events, context_scope="process", tags=["migration"], limit=5)
    assert [n["key"] for n, _v in picked] == ["team.rule"]            # the pack text takes no slot
    views, _ = kn.resolve_views_with_problems(events)
    shown = kn.select_knowledge_view(views, domains={}, registry_hash="rh", anchor_status_by_id={},
                                     lesson_limit=2)["lessons"]
    assert [n["key"] for n, _v in shown] == ["team.rule", "pack.demo:alpha"]    # and ranks after the team's
    assert shown[1][0]["body"].startswith("Do the thing.")            # the displayed version is the pack's


def test_a_proposal_shows_with_team_origin_when_uncurated_notes_are_included(tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha"), _line("beta")])
    _load(root)
    kn.append_event(Store(root), _team_event("pack.demo:alpha", body="Our proposal. Why: x. Check: y."))
    views, _ = kn.resolve_views_with_problems(_events(root))
    shown = kn.select_knowledge_view(views, domains={}, registry_hash="rh", anchor_status_by_id={},
                                     lesson_limit=1, include_uncurated=True)["lessons"]
    (note, _verdict), = shown
    assert note["key"] == "pack.demo:alpha" and note["view"] == "proposal"
    assert note["id"] not in kn.pack_origin_ids(views)                # the proposal is the team's
    curated = views[(PROCESS, "pack.demo:alpha")]["curated"]
    assert curated["id"] in kn.pack_origin_ids(views)                 # the verified version is still the pack's


def test_a_verified_team_republish_is_injected_and_ranks_first(tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha"), _line("beta")])
    _load(root)
    kn.append_event(Store(root), _team_event("pack.demo:alpha", body="Our version. Why: x. Check: y."))
    assert _verify(root, "pack.demo:alpha") == 0
    events = _events(root)
    picked = lc.select_lessons(events, context_scope="process", tags=["migration"], limit=5)
    assert [n["key"] for n, _v in picked] == ["pack.demo:alpha"] and picked[0][0]["body"].startswith("Our version.")
    views, _ = kn.resolve_views_with_problems(events)
    shown = kn.select_knowledge_view(views, domains={}, registry_hash="rh", anchor_status_by_id={},
                                     lesson_limit=1)["lessons"]
    assert shown[0][0]["body"].startswith("Our version.")


def test_every_event_carries_the_origin_of_the_publish_it_comes_from(tmp_path: Path, packs) -> None:
    root = _store(tmp_path)
    packs([_line("alpha")])
    _load(root)
    _verify(root, "pack.demo:alpha")                                   # a team curation of the pack content
    _unload(root)                                                       # not ours any more: kept for the team
    events = _events(root)
    views, _ = kn.resolve_views_with_problems(events)
    pack_ids = kn.pack_origin_ids(views)
    rec = views[(PROCESS, "pack.demo:alpha")]
    assert rec["curated"]["id"] in pack_ids and rec["latest"]["id"] in pack_ids


# ---- PR #524 review: an unloaded pack is not listed as loaded; a bad ledger is a clear error;
# the reserved name on rename

def test_pack_list_says_unloaded_after_an_unload_and_keeps_the_version_history(
        tmp_path: Path, packs, capsys) -> None:
    root = _store(tmp_path)
    packs([_line("alpha"), _line("beta")])
    _load(root)
    capsys.readouterr()
    assert _run(root, "knowledge", "pack", "list", "--json") == 0
    assert json.loads(capsys.readouterr().out)[0]["loaded"] is True
    assert _run(root, "knowledge", "pack", "unload", "demo") == 0
    capsys.readouterr()
    assert _run(root, "knowledge", "pack", "list", "--json") == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["loaded"] is False and row["loaded_version"] == 1 and row["live"] == 0
    assert _run(root, "knowledge", "pack", "list") == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.strip().startswith("demo"))
    assert "loaded v1" not in line and "unloaded (was v1)" in line
    assert _load(root) == 0
    capsys.readouterr()
    assert _run(root, "knowledge", "pack", "list", "--json") == 0
    assert json.loads(capsys.readouterr().out)[0]["loaded"] is True


_BAD_LEDGERS = [
    pytest.param({"schema_version": 1, "packs": {"demo": {"version": 1, "keys": []}}}, id="keys-is-a-list"),
    pytest.param({"schema_version": 1, "packs": {"demo": {"version": "1", "keys": {}}}}, id="version-is-a-string"),
    pytest.param({"schema_version": 1, "packs": {"demo": {"keys": {}}}}, id="version-missing"),
    pytest.param({"schema_version": 1, "packs": {"demo": {"version": True, "keys": {}}}}, id="version-is-a-bool"),
    pytest.param({"schema_version": 1, "packs": {"demo": {"version": 1, "keys": {"k": "loaded"}}}},
                 id="key-state-is-a-string"),
    pytest.param({"schema_version": 1, "packs": {"demo": {"version": 1, "keys": {"k": {"hash": 3}}}}},
                 id="key-state-field-not-text"),
    pytest.param({"schema_version": 1, "packs": {"demo": "loaded"}}, id="entry-is-a-string"),
    pytest.param({"schema_version": 99, "packs": {}}, id="newer-schema"),
    pytest.param({"packs": {}}, id="schema-missing"),
]


@pytest.mark.parametrize("ledger", _BAD_LEDGERS)
def test_a_malformed_ledger_is_a_clear_error_not_a_crash(tmp_path: Path, packs, ledger, capsys) -> None:
    root = _store(tmp_path)
    packs([_line("alpha")])
    path = Store(root).dir / "knowledge" / "packs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger), encoding="utf-8")
    with pytest.raises(kn.KnowledgeError, match="pack ledger"):
        kp.read_ledger(Store(root))
    for argv in (("list",), ("load", "demo"), ("unload", "demo")):
        assert _run(root, "knowledge", "pack", *argv) == 2
        assert "pack ledger" in capsys.readouterr().err


@pytest.mark.parametrize("new_name", ["agenttalk-pack", "Agenttalk-Pack", "AGENTTALK-PACK"])
def test_a_rename_cannot_take_the_reserved_name_in_any_case(tmp_path: Path, new_name: str) -> None:
    store = Store(tmp_path)
    store.init(["lead", "dev"])
    with pytest.raises(ValueError, match="reserved"):
        store.rename_agent("dev", new_name)
    assert "dev" in store.load_config()["agents"]
