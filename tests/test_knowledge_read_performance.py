"""Deterministic Git-call budgets and result parity for knowledge reads."""

import json
from datetime import datetime, timezone

import pytest
from test_knowledge import _git, _repo

from agenttalk import cli, doctor as dr, knowledge as kn
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
    assert remainder == ([f"  and {problem_count - 5} more ({dr.full_list_hint(store.root)})"]
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
@pytest.mark.parametrize("first_missing", [False, True])
def test_shared_curation_id_keeps_only_the_first_notes_anchor_result(
    action,
    include_stale,
    json_output,
    first_missing,
    tmp_path,
    capsys,
):
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

    # A later duplicate curation is refused, leaving that note uncurated.
    if first_missing:
        events = events[2:] + events[:2]
    first_key = events[0]["key"]
    second_key = events[2]["key"]
    for event in events:
        if event["event"] == kn.EVENT_CURATE:
            event["id"] = "kn-shared-curation"
    views, folded_problems = kn.resolve_views_with_problems(events)
    assert views[("cli", first_key)]["curated"] == events[1]
    assert views[("cli", second_key)]["latest"] == events[2]
    assert views[("cli", second_key)]["curated"] is None
    assert folded_problems == [
        {
            "line": 4,
            "error": (f"duplicate event id kn-shared-curation: cli/{first_key} (line 2) and cli/{second_key} (line 4)"),
        }
    ]
    kn.notes_path(store).write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    before = {p.relative_to(store.dir): p.read_bytes() for p in store.dir.rglob("*") if p.is_file()}
    loaded, problems = kn.read_events(store)
    assert loaded == events[:3]
    assert problems == folded_problems

    assert cli.main(arguments) == 0

    result = capsys.readouterr()
    assert result.err == ""
    assert {p.relative_to(store.dir): p.read_bytes() for p in store.dir.rglob("*") if p.is_file()} == before
    if not json_output:
        assert "1 ledger problem(s)" in result.out
        diagnostic = next(line for line in result.out.splitlines() if "duplicate event id" in line)
        assert all(value in diagnostic for value in ("kn-shared-curation", "cli/present", "cli/gone"))
        shown = [line for line in result.out.splitlines() if line.startswith("  [pointer]")]
        assert len(shown) == (1 if include_stale or not first_missing else 0)
        if shown:
            assert f"cli/{first_key} " in shown[0]
        return

    payload = json.loads(result.out)
    assert len(payload["problems"]) == 1
    problem = payload["problems"][0]
    assert problem["line"] == 4
    assert all(value in problem["error"] for value in (
        "duplicate event id", "kn-shared-curation", "cli/present", "cli/gone",
    ))
    expected = json.loads(clean.out)
    expected["notes"] = [row for row in expected["notes"] if row["key"] == first_key]
    expected["totals"]["notes"] = len(expected["notes"])
    expected["totals"]["all"] = len(expected["notes"])
    for row in expected["notes"]:
        row["id"] = "kn-shared-curation"
    assert {**payload, "problems": []} == expected
    assert [row["key"] for row in payload["notes"]] == ([first_key] if include_stale or not first_missing else [])
    rows = {row["key"]: row for row in payload["notes"]}
    if not first_missing:
        assert rows["present"]["id"] == "kn-shared-curation"
        assert rows["present"]["_verdict"]["stale_reasons"] == []
        assert rows["present"]["_verdict"]["hard_stale"] is False
    elif include_stale:
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
    hint = f"({doctor.full_list_hint(store.root)})"
    if problem_count > 5:
        assert f"; and {problem_count - 5} more {hint}" in text
    else:
        assert "more" not in text and hint not in text
    as_json = report.to_dict()["checks"][0]
    if problem_count:
        assert len(as_json["data"]["problems"]) == problem_count  # what the hint promises
    assert "more" not in as_json["details"]  # the JSON text is unchanged


# --- the hint is a command that works from anywhere, and says so for every way a store is chosen ----

def _two_projects(tmp_path, problems=7):
    """Project A (healthy, the folder we stand in) and project B (damaged; its name has a space)."""
    a, b = tmp_path / "project a", tmp_path / "project b"
    for root in (a, b):
        root.mkdir()
        Store(root).init(["alpha", "beta"])
    _seed(Store(b), count=1, curated=True)
    with kn.notes_path(Store(b)).open("a", encoding="utf-8") as handle:
        handle.write(("{broken" + chr(10)) * problems)
    return a, b


def _printed_command(text):
    import re
    import shlex

    found = re.findall(r"\(full list: (agenttalk [^)]*)\)", text)
    assert len(found) == 1, text
    words = shlex.split(found[0])  # what a POSIX shell (and PowerShell, for single quotes) would pass
    assert words[0] == "agenttalk"
    return words[1:]


def _problems_in_the_doctor_json(capsys, words):
    assert cli.main(words) in (0, 2)  # 2: the doctor reports other errors of a bare test project
    checks = json.loads(capsys.readouterr().out)["checks"]
    return [c for c in checks if c["name"] == "knowledge_notes"]


@pytest.mark.parametrize("how", ["--root", "AGENTTALK_ROOT"])
@pytest.mark.parametrize("action", ["search", "pull", "onboard", "doctor"])
def test_the_hint_reaches_the_selected_project_from_another_one(action, how, tmp_path, monkeypatch, capsys):
    a, b = _two_projects(tmp_path)
    monkeypatch.chdir(a)
    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    arguments = ["knowledge", action] + (["sample"] if action == "search" else [])
    if action == "doctor":
        arguments = ["doctor"]
    if how == "--root":
        arguments = ["--root", str(b), *arguments]
    else:
        monkeypatch.setenv("AGENTTALK_ROOT", str(b))
    assert cli.main(arguments) in (0, 2)
    words = _printed_command(capsys.readouterr().out)
    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)  # the reader runs it from project A, with no help
    found = _problems_in_the_doctor_json(capsys, words)
    assert len(found) == 1 and len(found[0]["data"]["problems"]) == 7  # B's whole list, not A's


# --- a root that could be read as shell syntax is never printed inside a command ----------------------

_QUOTES = {"ascii apostrophe": "'", "U+2018": "\u2018", "U+2019": "\u2019", "U+201A": "\u201a",
           "U+201B": "\u201b", "U+201C": "\u201c", "U+201D": "\u201d", "U+201E": "\u201e"}


def _shells():
    import shutil
    from pathlib import Path as P

    found = {}
    if shutil.which("pwsh"):
        found["pwsh"] = lambda text: [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command",
                                      "function agenttalk { 'STUB' }; " + text]
    git_bash = P("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.exists() else shutil.which("bash")
    if bash:
        found["bash"] = lambda text: [bash, "-c", "agenttalk() { echo STUB; }; " + text]
    return found


def _hint_text(printed):
    """What follows "full list: " up to the closing bracket of the hint: what a reader would paste."""
    import re

    found = re.findall(r"\(full list: (.*)\)\s*$", printed, flags=re.M)
    assert len(found) == 1, printed
    return found[0]


@pytest.mark.parametrize("quote", list(_QUOTES.values()), ids=list(_QUOTES))
@pytest.mark.parametrize("action", ["search", "pull", "onboard", "doctor"])
def test_a_root_with_a_shell_quote_character_is_named_as_data_and_runs_nothing(
        action, quote, tmp_path, monkeypatch, capsys):
    import subprocess

    shells = _shells()
    if not shells:
        pytest.skip("no PowerShell 7 or bash on this machine")
    payload = f"proj{quote}; echo HINT_MARKER; #"
    a, b = tmp_path / "a", tmp_path / payload
    for root in (a, b):
        root.mkdir()
        Store(root).init(["alpha", "beta"])
    _seed(Store(b), count=1, curated=True)
    with kn.notes_path(Store(b)).open("a", encoding="utf-8") as handle:
        handle.write(("{broken" + chr(10)) * 7)
    monkeypatch.chdir(a)
    arguments = ["doctor"] if action == "doctor" else ["knowledge", action] + (["sample"] if action == "search" else [])
    assert cli.main(["--root", str(b), *arguments]) in (0, 2)
    text = _hint_text(capsys.readouterr().out)

    assert "run with --root set to the project at" in text  # the prose form, not a command
    assert "'" not in text and quote not in text and ";" not in text  # nothing a shell could read as syntax
    for name, command in shells.items():
        done = subprocess.run(command(text), capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
        assert "HINT_MARKER" not in done.stdout, (name, done.stdout)


@pytest.mark.parametrize("name", ["plain", "with space", "caf\u00e9 \u00fc\u00f1\u00ed",
                                  "\u4e2d\u6587 \u0434\u0438\u0440"])
def test_a_root_of_letters_digits_and_the_allowed_marks_is_a_copyable_command(name, tmp_path):
    root = tmp_path / name
    hint = dr.full_list_hint(root)
    assert hint == f"full list: agenttalk --root '{root.as_posix()}' doctor --json"
    for shell, command in _shells().items():
        import subprocess

        done = subprocess.run(command(hint.removeprefix("full list: ")), capture_output=True, text=True,
                              timeout=60, cwd=str(tmp_path), encoding="utf-8")
        assert done.stdout.strip() == "STUB", (shell, done.stdout, done.stderr)


@pytest.mark.parametrize("char", list("$`;&|()<>{}[]\"'!#%^*?\u2018\u2019\u201a\u201b\u201c\u201d\u201e\n\t"))
def test_any_character_outside_the_allow_list_turns_the_hint_into_prose(char, tmp_path):
    hint = dr.full_list_hint(f"{tmp_path.as_posix()}/x{char}y")
    assert hint.startswith("full list: agenttalk doctor --json, run with --root set to the project at ")
    assert "'" not in hint
    assert hint.count('"') == 2  # only the two that wrap the root
    assert char == '"' or char not in hint[len("full list: "):]
    assert f"\\u{ord(char):04x}" in hint


def test_the_prose_names_the_project_for_every_way_a_store_is_chosen(tmp_path):
    hint = dr.full_list_hint(tmp_path / "a b$c")
    assert "run with --root set to the project at" in hint and "a b\\u0024c" in hint
    assert "same --root option" not in hint  # no option the reader never gave



# --- #663: the prose form keeps two different folders apart, and describes its own code forms -------

def _decode_the_prose(hint):
    """Read the root back out of the printed prose: the text in the two double quotes, with each
    code (a backslash, `u` and four hex digits, or a backslash, `U` and eight) turned back into its character."""
    import re

    quoted = re.fullmatch(r'full list: agenttalk doctor --json, run with --root set to the project at "(.*)", '
                          r'with every other character, and every backslash, written as .*', hint)
    assert quoted, hint
    body = quoted.group(1)
    assert "\\" not in re.sub(r"\\u[0-9a-f]{4}|\\U[0-9a-f]{8}", "", body)  # every backslash starts a code
    return re.sub(r"\\u([0-9a-f]{4})|\\U([0-9a-f]{8})", lambda m: chr(int(m.group(1) or m.group(2), 16)), body)


def test_two_folders_that_differ_only_by_a_literal_backslash_print_differently():
    from pathlib import PurePosixPath

    one = dr.full_list_hint(PurePosixPath("/srv/a\\u0024$"))
    other = dr.full_list_hint(PurePosixPath("/srv/a$\\u0024"))
    assert one != other
    assert _decode_the_prose(one) == "/srv/a\\u0024$" and _decode_the_prose(other) == "/srv/a$\\u0024"


def test_every_root_prints_text_that_decodes_back_to_it_and_no_two_print_alike():
    import random
    from pathlib import PurePosixPath

    alphabet = ["a", "Z", "7", " ", "-", ".", "/", "\\", "\\", "u", "U", "0", "f", "$", "`", ";", "'", '"',
                "’", "“", "é", "中", "\U0001f600", "\U0001f1ff", "퟿", "￿", "\n"]
    rng = random.Random(663)  # noqa: S311  (a fixed seed: the same roots every run)
    roots = {"/", "/srv/a\\u0024$", "/srv/a$\\u0024", "/a\\u0024", "/a\\\\u0024", "/a\\U0001f600", "/a\U0001f600",
             "/a\\", "/a\\\\", "/a\\u", "/\\u005c", "/\\", "/\\U0001f600$"}
    while len(roots) < 4000:
        roots.add("/" + "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 9))))
    printed = {}
    for text in sorted({PurePosixPath(raw).as_posix() for raw in roots}):  # one root, one spelling
        hint = dr.full_list_hint(PurePosixPath(text))
        assert hint not in printed, (text, printed.get(hint))  # no two roots print alike
        printed[hint] = text
        if hint.startswith("full list: agenttalk --root '"):  # the command form: the root, quoted, as it is
            assert hint == f"full list: agenttalk --root '{text}' doctor --json"
        else:
            assert _decode_the_prose(hint) == text, (text, hint)


def test_the_prose_explains_both_code_forms_and_the_backslash():
    hint = dr.full_list_hint("/srv/a$\U0001f600")
    assert "\\u0024" in hint and "\\U0001f600" in hint  # a short and a long code are both used
    assert hint.endswith("written as \\uXXXX with four hex digits, or \\UXXXXXXXX with eight above U+FFFF")
    assert "every backslash" in hint
