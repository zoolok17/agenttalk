"""Deterministic Git-call budgets and result parity for knowledge reads."""

import json
from datetime import datetime, timezone

import pytest
from test_knowledge import _git, _repo

from agenttalk import cli, knowledge as kn
from agenttalk.store import Store

SHA = "a" * 40


@pytest.mark.parametrize("action", ["search", "pull", "onboard"])
@pytest.mark.parametrize("type_filter", [None, "lesson", "pointer"])
@pytest.mark.parametrize("problem_count", [0, 1, 5, 6, 103])
def test_problem_text_is_capped_without_changing_json_or_notes(
    action, type_filter, problem_count, store, monkeypatch, capsys,
):
    _seed(store, count=2, curated=True)
    _count_git(monkeypatch)
    arguments = ["--root", str(store.root), "knowledge", action]
    if type_filter:
        arguments.extend(["--type", type_filter])
    if action == "search":
        arguments.append("sample")
    assert cli.main([*arguments, "--json"]) == 0
    clean = json.loads(capsys.readouterr().out)
    with kn.notes_path(store).open("a", encoding="utf-8") as handle:
        handle.write("{broken\n" * problem_count)
    before = kn.notes_path(store).read_bytes()

    assert cli.main(arguments) == 0
    output = capsys.readouterr().out

    details = [line for line in output.splitlines() if line.startswith("  line ")]
    assert [line.split(":", 1)[0] for line in details] == [
        f"  line {number}" for number in range(5, 5 + min(problem_count, 5))
    ]
    if problem_count:
        assert f"{problem_count} ledger problem(s) (see doctor)" in output
    else:
        assert "ledger problem" not in output
    remainder = [line for line in output.splitlines() if line.startswith("  and ")]
    assert remainder == ([f"  and {problem_count - 5} more (full list: agenttalk doctor --json)"]
                          if problem_count > 5 else [])

    assert cli.main([*arguments, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    if type_filter:
        assert payload == clean
    else:
        assert [problem["line"] for problem in payload["problems"]] == list(range(5, 5 + problem_count))
        assert all(set(problem) == {"line", "error"} for problem in payload["problems"])
        assert all(problem["error"].startswith("invalid json:") for problem in payload["problems"])
        assert {**payload, "problems": []} == clean
    assert kn.notes_path(store).read_bytes() == before


@pytest.mark.parametrize("action", ["search", "pull", "onboard"])
@pytest.mark.parametrize("include_stale", [False, True])
@pytest.mark.parametrize("json_output", [False, True])
def test_shared_curation_id_keeps_each_notes_anchor_result(action, include_stale, json_output, tmp_path, capsys):
    root = _repo(tmp_path)
    gone = root / "src" / "gone.py"
    gone.write_text("removed source\n", encoding="utf-8")
    _git(root, "add", "src/gone.py")
    _git(root, "commit", "-qm", "add second anchor")
    baseline = _git(root, "rev-parse", "HEAD").strip()
    gone.unlink()
    _git(root, "commit", "-qam", "remove second anchor")
    store = Store(root)
    registry = cli._load_domain_registry(store)
    events = []
    for key, path in [("present", "src/cli.py"), ("gone", "src/gone.py")]:
        note = kn.new_publish_event(
            note_id=f"kn-{key}",
            key=key,
            type="pointer",
            domain_id="cli",
            body="sample anchor",
            anchor={"kind": "path", "path": path},
            verified_against_sha=baseline,
            domain_registry_hash=registry.registry_hash,
            domain_definition_hash=kn.domain_definition_hash(registry.data["domains"]["cli"]),
            author="dev",
            resolved_from="active_agent",
            at="t1",
        )
        curated = kn.new_curate_event(
            base=note,
            action="verify",
            curated_by="curator",
            resolved_from="curator",
            at="t2",
            reason=None,
            domain_registry_hash=registry.registry_hash,
        )
        curated["id"] = f"kn-curation-{key}"
        events.extend([note, curated])
    kn.knowledge_dir(store).mkdir(parents=True, exist_ok=True)
    kn.notes_path(store).write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    arguments = ["--root", str(root), "knowledge", action]
    if json_output:
        arguments.append("--json")
    if include_stale:
        arguments.append("--include-stale")
    if action == "search":
        arguments.append("sample")

    assert cli.main(arguments) == 0
    clean = capsys.readouterr()
    assert clean.err == ""
    if json_output:
        assert json.loads(clean.out)["problems"] == []
    else:
        assert "ledger problem" not in clean.out

    # Change only the event ids: each curation still binds to its own publish.
    for event in events:
        if event["event"] == kn.EVENT_CURATE:
            event["id"] = "kn-shared-curation"
    assert kn.resolve_views_with_problems(events)[1] == []
    kn.notes_path(store).write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    before = {p.relative_to(store.dir): p.read_bytes() for p in store.dir.rglob("*") if p.is_file()}

    assert cli.main(arguments) == 0

    result = capsys.readouterr()
    assert result.err == ""
    assert {p.relative_to(store.dir): p.read_bytes() for p in store.dir.rglob("*") if p.is_file()} == before
    if not json_output:
        assert "1 ledger problem(s)" in result.out
        diagnostic = next(line for line in result.out.splitlines() if "duplicate event id" in line)
        assert all(value in diagnostic for value in ("kn-shared-curation", "cli/present", "cli/gone"))
        unchanged = "\n".join(line for line in result.out.splitlines() if line != diagnostic) + "\n"
        assert unchanged.replace("; 1 ledger problem(s) (see doctor)", "") == clean.out
        return

    payload = json.loads(result.out)
    assert len(payload["problems"]) == 1
    problem = payload["problems"][0]
    assert problem["line"] == 4
    assert all(value in problem["error"] for value in (
        "duplicate event id", "kn-shared-curation", "cli/present", "cli/gone",
    ))
    expected = json.loads(clean.out)
    for row in expected["notes"]:
        row["id"] = "kn-shared-curation"
    assert {**payload, "problems": []} == expected
    assert [row["key"] for row in payload["notes"]] == (["gone", "present"] if include_stale else ["present"])
    rows = {row["key"]: row for row in payload["notes"]}
    assert set(rows) == ({"present", "gone"} if include_stale else {"present"})
    assert rows["present"]["id"] == "kn-shared-curation"
    assert rows["present"]["_verdict"]["stale_reasons"] == []
    assert rows["present"]["_verdict"]["hard_stale"] is False
    if include_stale:
        assert rows["gone"]["id"] == "kn-shared-curation"
        assert kn.STALE_ANCHOR_GONE in rows["gone"]["_verdict"]["stale_reasons"]
        assert rows["gone"]["_verdict"]["hard_stale"] is True


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


@pytest.mark.parametrize("problem_count", [0, 5, 6, 103])
def test_doctor_text_says_where_the_rest_of_a_shortened_problem_list_is(problem_count, store, capsys):
    """The hint is true: the doctor's own JSON holds every problem, and its text adds the hint only
    when it shows fewer than all of them."""
    from agenttalk import doctor

    _seed(store, count=1, curated=True)
    with kn.notes_path(store).open("a", encoding="utf-8") as handle:
        handle.write(("{broken" + chr(10)) * problem_count)
    check = doctor._check_knowledge(store)
    report = doctor.Report(agenttalk_version="x", python_version="3", project_root=str(store.root),
                           agenttalk_module_path="", store_schema_capabilities=[], checks=[check])
    cli._render_doctor_human(report)
    text = capsys.readouterr().out
    hint = "(full list: agenttalk doctor --json)"
    if problem_count > 5:
        assert f"; and {problem_count - 5} more {hint}" in text
    else:
        assert "more" not in text and hint not in text
    as_json = report.to_dict()["checks"][0]
    if problem_count:
        assert len(as_json["data"]["problems"]) == problem_count  # what the hint promises
    assert "more" not in as_json["details"]  # the JSON text is unchanged

