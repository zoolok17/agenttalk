"""Tests for the project onboarding ledger."""

from __future__ import annotations

import json
from pathlib import Path

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
