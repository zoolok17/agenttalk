"""Tests for the project onboarding ledger."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier

import pytest

from agenttalk import cli, onboarding as ob
from agenttalk.store import Store


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _create(store: Store, *, run_id: str = "ob-main") -> dict:
    evt = ob.new_create_event(
        run_id=run_id,
        title="Existing project analysis",
        objective="Map the codebase before assigning implementation.",
        base_ref="main",
        lead="alpha",
        state="scanning",
        at="2026-07-09T10:00:00Z",
    )
    ob.append_event(store, evt)
    return evt


def test_onboarding_fold_counts_latest_records(store: Store) -> None:
    _create(store)
    ob.append_event(store, ob.new_record_event(
        run_id="ob-main",
        kind=ob.KIND_SEGMENT,
        key="api",
        status="accepted",
        summary="API surface mapped.",
        actor="alpha",
        owner="alpha",
        checkers=["beta"],
        paths=["src/agenttalk/cli.py"],
        at="2026-07-09T10:01:00Z",
    ))
    ob.append_event(store, ob.new_record_event(
        run_id="ob-main",
        kind=ob.KIND_CLAIM,
        key="docs.match.api",
        status="conflicted",
        summary="README command list differs from parser help.",
        actor="beta",
        segment="api",
        source="docs",
        confidence="high",
        at="2026-07-09T10:02:00Z",
    ))
    ob.append_event(store, ob.new_record_event(
        run_id="ob-main",
        kind=ob.KIND_UNKNOWN,
        key="release.owner",
        status="open",
        summary="Need human confirmation of release owner.",
        actor="beta",
        blocking=True,
        at="2026-07-09T10:03:00Z",
    ))
    ob.append_event(store, ob.new_state_event(
        run_id="ob-main",
        state="blocked",
        actor="alpha",
        summary="Waiting for ownership answer.",
        at="2026-07-09T10:04:00Z",
    ))

    run, problems = ob.get_run(store, "ob-main")

    assert problems == []
    assert run is not None
    assert run["state"] == "blocked"
    assert run["blocked"] is True
    assert run["counts"]["accepted_segments"] == 1
    assert run["counts"]["conflicted_claims"] == 1
    assert run["counts"]["blocking_unknowns"] == 1
    assert run["records"]["claim"][0]["source"] == "docs"


def test_onboarding_rejects_unsafe_paths(store: Store) -> None:
    _create(store)
    with pytest.raises(ob.OnboardingError):
        ob.new_record_event(
            run_id="ob-main",
            kind=ob.KIND_SEGMENT,
            key="bad",
            status="assigned",
            summary="bad path",
            actor="alpha",
            paths=["../outside.py"],
        )


def test_onboarding_reader_skips_corrupt_lines(store: Store) -> None:
    _create(store)
    path = ob.events_path(store, "ob-main")
    path.write_text(path.read_text(encoding="utf-8") + "{bad-json\n", encoding="utf-8")

    events, problems = ob.read_events(store, "ob-main")
    run = ob.run_view(events)

    assert run is not None
    assert run["title"] == "Existing project analysis"
    assert problems and problems[0]["line"] == 2


def test_onboarding_reader_rejects_malformed_list_fields(store: Store) -> None:
    _create(store)
    evt = ob.new_record_event(
        run_id="ob-main",
        kind=ob.KIND_SEGMENT,
        key="cli",
        status="accepted",
        summary="CLI mapped.",
        actor="alpha",
        checkers=["beta"],
    )
    evt["checkers"] = "beta"
    path = ob.events_path(store, "ob-main")
    path.write_text(
        path.read_text(encoding="utf-8") + json.dumps(evt) + "\n",
        encoding="utf-8",
    )

    events, problems = ob.read_events(store, "ob-main")
    run = ob.run_view(events)

    assert run is not None
    assert run["counts"]["segments"] == 0
    assert problems and "checker must be a list" in problems[0]["error"]


def test_onboarding_append_after_unterminated_tail_preserves_new_event(store: Store) -> None:
    _create(store)
    path = ob.events_path(store, "ob-main")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"event": "state", "run_id": "ob-main"')
    state = ob.new_state_event(
        run_id="ob-main",
        state="ready-for-work",
        actor="alpha",
        at="2026-07-09T10:05:00Z",
    )

    ob.append_event(store, state)

    events, problems = ob.read_events(store, "ob-main")
    assert [event["event"] for event in events] == [ob.EVENT_CREATE, ob.EVENT_STATE]
    assert len(problems) == 1 and problems[0]["line"] == 2


def test_onboarding_append_after_invalid_utf8_tail_preserves_valid_events(
    store: Store,
) -> None:
    _create(store)
    path = ob.events_path(store, "ob-main")
    with open(path, "ab") as fh:
        fh.write(b'{"event":"state","summary":"\xe2')
    state = ob.new_state_event(
        run_id="ob-main",
        state="ready-for-work",
        actor="alpha",
        at="2026-07-09T10:05:00Z",
    )

    ob.append_event(store, state)

    events, problems = ob.read_events(store, "ob-main")
    assert [event["event"] for event in events] == [ob.EVENT_CREATE, ob.EVENT_STATE]
    assert problems == [{"line": 2, "error": "invalid utf-8"}]


def test_onboarding_reader_rejects_non_object_event_without_raising(store: Store) -> None:
    _create(store)
    path = ob.events_path(store, "ob-main")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(["not", "an", "object"]) + "\n")

    events, problems = ob.read_events(store, "ob-main")

    assert len(events) == 1
    assert problems == [{"line": 2, "error": "not a JSON object"}]


def test_onboarding_reader_rejects_string_false_for_blocking(store: Store) -> None:
    _create(store)
    event = ob.new_record_event(
        run_id="ob-main",
        kind=ob.KIND_UNKNOWN,
        key="release.owner",
        status="open",
        summary="Need an owner.",
        actor="alpha",
        blocking=False,
        at="2026-07-09T10:05:00Z",
    )
    event["blocking"] = "false"
    path = ob.events_path(store, "ob-main")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")

    events, problems = ob.read_events(store, "ob-main")
    run = ob.run_view(events)

    assert run is not None and run["counts"]["unknowns"] == 0
    assert problems == [{"line": 2, "error": "blocking must be a boolean"}]


def test_onboarding_reader_streams_without_path_read_text(
    store: Store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create(store)

    def fail_read_text(*args, **kwargs):
        raise AssertionError("onboarding ledger must be streamed")

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    events, problems = ob.read_events(store, "ob-main")
    assert len(events) == 1 and problems == []


def test_create_run_serializes_duplicate_concurrent_creates(store: Store) -> None:
    event = ob.new_create_event(
        run_id="ob-race",
        title="Concurrent create",
        objective=None,
        base_ref="HEAD",
        lead="alpha",
        state="scanning",
        at="2026-07-09T10:00:00Z",
    )

    def create_once(_: int) -> bool:
        try:
            ob.create_run(store, event)
        except ob.OnboardingError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(create_once, range(8)))

    events, problems = ob.read_events(store, "ob-race")
    assert sum(outcomes) == 1
    assert problems == []
    assert [row["event"] for row in events] == [ob.EVENT_CREATE]


def test_onboarding_cli_create_record_show_json(store_root: Path, capsys: pytest.CaptureFixture) -> None:
    rc = _run([
        "onboarding", "create",
        "--id", "ob-cli",
        "--from", "alpha",
        "--title", "CLI onboarding",
        "--objective", "Map project before work.",
        "--base-ref", "HEAD",
        "--json",
    ], store_root)
    assert rc == 0
    created = json.loads(capsys.readouterr().out)
    assert created["id"] == "ob-cli"

    rc = _run([
        "onboarding", "record",
        "--id", "ob-cli",
        "--from", "beta",
        "--kind", "segment",
        "--key", "cli",
        "--status", "accepted",
        "--summary", "CLI command surface mapped.",
        "--path", "src/agenttalk/cli.py",
        "--checker", "alpha",
        "--json",
    ], store_root)
    assert rc == 0
    row = json.loads(capsys.readouterr().out)
    assert row["paths"] == ["src/agenttalk/cli.py"]

    rc = _run(["onboarding", "show", "--id", "ob-cli", "--json"], store_root)
    assert rc == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["counts"]["accepted_segments"] == 1
    assert shown["records"]["segment"][0]["key"] == "cli"


def test_onboarding_cli_duplicate_create_refuses_existing_run(
    store_root: Path, capsys: pytest.CaptureFixture
) -> None:
    argv = [
        "onboarding", "create",
        "--id", "ob-dupe",
        "--from", "alpha",
        "--title", "Duplicate test",
    ]
    assert _run(argv, store_root) == 0
    capsys.readouterr()

    rc = _run(argv, store_root)

    assert rc == 2
    assert "already exists" in capsys.readouterr().err


def test_onboarding_cli_concurrent_create_writes_exactly_one_event(
    store_root: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv = [
        "onboarding", "create",
        "--id", "ob-cli-race",
        "--from", "alpha",
        "--title", "Concurrent CLI create",
    ]
    obsolete_append_barrier = Barrier(2)
    append_event = ob.append_event

    def synchronized_obsolete_append(store: Store, event: dict) -> None:
        obsolete_append_barrier.wait(timeout=5)
        append_event(store, event)

    monkeypatch.setattr(ob, "append_event", synchronized_obsolete_append)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: _run(argv, store_root), range(2)))

    events, problems = ob.read_events(Store(store_root), "ob-cli-race")
    captured = capsys.readouterr()
    assert sorted(outcomes) == [0, 2]
    assert "already exists" in captured.err
    assert problems == []
    assert [event["event"] for event in events] == [ob.EVENT_CREATE]


def test_onboarding_cli_list_and_state_json(store_root: Path, capsys: pytest.CaptureFixture) -> None:
    assert _run([
        "onboarding", "create",
        "--id", "ob-state",
        "--from", "alpha",
        "--title", "State test",
    ], store_root) == 0
    capsys.readouterr()

    rc = _run([
        "onboarding", "state",
        "--id", "ob-state",
        "--from", "beta",
        "--state", "ready-for-work",
        "--summary", "required segments accepted",
        "--json",
    ], store_root)
    assert rc == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["state"] == "ready-for-work"
    assert updated["state_summary"] == "required segments accepted"

    rc = _run(["onboarding", "list", "--json"], store_root)
    assert rc == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["total"] == 1
    assert listed["runs"][0]["id"] == "ob-state"
    assert listed["runs"][0]["state"] == "ready-for-work"


def test_onboarding_cli_rejects_path_escape(store_root: Path, capsys: pytest.CaptureFixture) -> None:
    assert _run([
        "onboarding", "create",
        "--id", "ob-bad-path",
        "--from", "alpha",
        "--title", "Bad path test",
    ], store_root) == 0
    capsys.readouterr()

    rc = _run([
        "onboarding", "record",
        "--id", "ob-bad-path",
        "--from", "alpha",
        "--kind", "segment",
        "--key", "bad",
        "--status", "assigned",
        "--summary", "bad path",
        "--path", "../outside.py",
    ], store_root)

    assert rc == 2
    assert "not safe repo-relative path" in capsys.readouterr().err
