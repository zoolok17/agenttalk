"""Deterministic Git-call budgets and result parity for knowledge reads."""

import json
from datetime import datetime, timezone

import pytest

from agenttalk import cli, knowledge as kn

SHA = "a" * 40


def _seed(store, *, count=268, curated=False):
    registry = cli._load_domain_registry(store)
    events = []
    for index in range(count):
        note = kn.new_publish_event(
            note_id=f"kn-pointer-{index}",
            key=f"pointer-{index:03}",
            type="pointer",
            domain_id="process",
            body="sample pointer",
            anchor={"kind": "path", "path": "sample.py"},
            verified_against_sha=SHA if curated else None,
            domain_registry_hash=registry.registry_hash,
            domain_definition_hash=kn.VIRTUAL_PROCESS_DOMAIN_HASH,
            author="alpha",
            resolved_from="active_agent",
            at="t1",
        )
        events.append(note)
        if curated:
            events.append(
                kn.new_curate_event(
                    base=note,
                    action="verify",
                    curated_by="alpha",
                    resolved_from="lead",
                    at="t2",
                    reason=None,
                    domain_registry_hash=registry.registry_hash,
                )
            )
    kn.knowledge_dir(store).mkdir(parents=True, exist_ok=True)
    kn.notes_path(store).write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def _count_git(monkeypatch):
    calls = []

    def git(root, arguments):
        calls.append(tuple(arguments))
        return (0, SHA) if arguments == ["rev-parse", "HEAD"] else (0, "")

    monkeypatch.setattr(cli, "_git", git)
    return calls


@pytest.mark.parametrize(
    "arguments",
    [
        ["search", "--type", "lesson", "--limit", "5", "--", "missing"],
        ["pull"],
        ["onboard"],
    ],
)
def test_unshown_pointers_need_no_git(arguments, store, monkeypatch, capsys):
    _seed(store)
    calls = _count_git(monkeypatch)

    assert cli.main(["--root", str(store.root), "knowledge", arguments[0], "--json", *arguments[1:]]) == 0

    assert len(calls) == 0, len(calls)
    payload = json.loads(capsys.readouterr().out)
    assert payload == [] if arguments[0] == "search" else payload["notes"] == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["search", "--type", "lesson", "--", "missing"],
        ["search", "--", "missing"],
        ["pull", "--domain", "other"],
        ["pull", "--type", "lesson"],
        ["onboard", "--type", "lesson"],
    ],
)
def test_safe_filters_precede_anchor_checks(arguments, store, monkeypatch, capsys):
    _seed(store, curated=True)
    calls = _count_git(monkeypatch)
    # Put --json before a possible end-of-options separator.
    assert cli.main(["--root", str(store.root), "knowledge", arguments[0], "--json", *arguments[1:]]) == 0
    capsys.readouterr()
    assert len(calls) == 0, len(calls)


@pytest.mark.parametrize("action", ["pull", "onboard", "search"])
def test_repeated_repository_facts_are_shared_only_within_one_read(action, store, monkeypatch, capsys):
    _seed(store, curated=True)
    calls = _count_git(monkeypatch)
    arguments = ["--root", str(store.root), "knowledge", action, "--json"]
    if action == "search":
        arguments.append("sample")
    for _ in range(2):
        calls.clear()
        assert cli.main(arguments) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["totals"]["notes"] == 268
        assert len(calls) == 4, len(calls)
        assert len(set(calls)) == 4


def test_mixed_read_preserves_order_flags_and_ledger_problems(store, monkeypatch, capsys):
    _seed(store, count=3, curated=True)
    events, _ = kn.read_events(store)
    # Give the three pointers distinct Git states, without altering their schema.
    for index in range(3):
        publish = events[index * 2]
        publish["anchor"]["path"] = ["sample.py", "changed.py", "missing.py"][index]
        events[index * 2 + 1] = kn.new_curate_event(
            base=publish,
            action="verify",
            curated_by="alpha",
            resolved_from="lead",
            at="t2",
            reason=None,
            domain_registry_hash=publish["domain_registry_hash"],
        )
    other = {**events[0], "id": "kn-other", "domain_id": "other", "key": "other-pointer"}
    events.extend(
        [
            other,
            kn.new_curate_event(
                base=other,
                action="verify",
                curated_by="alpha",
                resolved_from="lead",
                at="t2",
                reason=None,
            ),
        ]
    )
    lesson = kn.new_publish_event(
        note_id="kn-lesson",
        key="lesson",
        type="lesson",
        domain_id="process",
        body="sample lesson",
        anchor=None,
        verified_against_sha=None,
        domain_registry_hash=events[0]["domain_registry_hash"],
        domain_definition_hash=kn.VIRTUAL_PROCESS_DOMAIN_HASH,
        author="alpha",
        resolved_from="active_agent",
        at="2026-11-03T00:00:00Z",
        lesson={
            "scope": "process",
            "trigger": "sample trigger",
            "evidence_ref": "sample evidence",
            "review_after": "2026-11-04",
            "expires_at": "2030-01-01",
        },
    )
    events.extend(
        [
            lesson,
            kn.new_curate_event(
                base=lesson,
                action="verify",
                curated_by="alpha",
                resolved_from="lead",
                at="2026-11-03T00:01:00Z",
                reason=None,
            ),
        ]
    )
    events.append({**events[1], "id": "kn-forged", "body": "forged body"})
    kn.notes_path(store).write_text(
        "".join(json.dumps(event) + "\n" for event in events) + "not json\n",
        encoding="utf-8",
    )
    calls = _count_git(monkeypatch)
    git = cli._git

    def mixed_git(root, arguments):
        if arguments[:2] == ["cat-file", "-e"] and arguments[-1] == "HEAD:missing.py":
            return 1, ""
        if arguments[0] == "diff" and arguments[-1] == "changed.py":
            return 0, "changed.py\n"
        return git(root, arguments)

    monkeypatch.setattr(cli, "_git", mixed_git)
    monkeypatch.setattr(kn, "_coerce_now", lambda now=None: datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert cli.main(["--root", str(store.root), "knowledge", "pull", "--json", "--include-stale"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [row["key"] for row in payload["notes"]] == [
        "other-pointer",
        "pointer-000",
        "pointer-001",
        "pointer-002",
    ]
    verdicts = [row["_verdict"] for row in payload["notes"][1:]]
    assert not verdicts[0]["hard_stale"]
    assert kn.STALE_ANCHOR_CHANGED in verdicts[1]["stale_reasons"]
    assert kn.STALE_ANCHOR_GONE in verdicts[2]["stale_reasons"]
    assert payload["problems"]
    assert [row["key"] for row in payload["lessons"]] == ["lesson"]
    assert len(payload["problems"]) == 2
    assert calls
