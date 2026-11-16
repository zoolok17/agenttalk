"""Damaged cleanliness values hold with executable repair guidance (#798)."""

import json
import re
import unicodedata

import pytest

from agenttalk import acceptance, cli, close, printed_commands as pc
import test_acceptance as legacy
import test_acceptance_staging as staging_tests
from test_acceptance_staging import candidate, staged, environment, observation, plan, registry  # noqa: F401
from test_close import SHA, _gate_go, _satisfied

case, case_v2, case_v3 = legacy.case, legacy.case_v2, legacy.case_v3


BAD = ["false", "true", "yes", "", 1, 0, 1.0, 0.0, [], [True], {}, {"a": 1}, None]


def detail(record):
    result = close.compute_verdict(record, _gate_go())
    assert result["verdict"] == "HOLD"
    return next(h["detail"] for h in result["holds"] if h["code"] == close.HOLD_REVISION)


@pytest.mark.parametrize("value", BAD)
@pytest.mark.parametrize("artifact", [None, "pointer:diff"])
def test_damaged_cleanliness_holds_even_with_artifact(value, artifact):
    record = _satisfied()
    assert close.compute_verdict(record, _gate_go())["verdict"] == "GO"
    record.update(revision_clean=value, dirty_artifact=artifact)
    text = detail(record)
    assert "revision_clean" in text and "damaged" in text
    assert "was dirty with no recorded" not in text
    assert f"close reopen --id c1 --revision {SHA} --from <lead>" in text
    assert "at that revision" in text
    assert "\n" not in text
    display = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    assert pc.shown(display, limit=60) in text


@pytest.mark.parametrize("artifact", [None, "pointer:diff"])
def test_missing_cleanliness_is_damage(artifact):
    record = _satisfied()
    record.pop("revision_clean")
    record["dirty_artifact"] = artifact
    assert "damaged" in detail(record)


@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize("artifact", [None, "pointer:diff"])
def test_boolean_cleanliness_keeps_existing_contract(value, artifact):
    record = dict(_satisfied(), revision_clean=value, dirty_artifact=artifact)
    result = close.compute_verdict(record, _gate_go())
    assert result["ok"] is (value or bool(artifact))
    if not result["ok"]:
        assert detail(record) == "revision worktree was dirty with no recorded diff artifact"


def test_damage_display_is_distinct_bounded_and_one_line():
    assert len({detail(dict(_satisfied(), revision_clean=v)) for v in [0, 1, 0.0, 1.0]}) == 4
    text = detail(dict(_satisfied(), revision_clean="x" * 1000))
    assert "x" * 61 not in text and "cut here" in text
    for value in ["false", "line\n\x1b\u2028\u202e"]:
        text = detail(dict(_satisfied(), revision_clean=value))
        assert "(text)" in text
        assert not any(unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} for ch in text)


@pytest.mark.parametrize(
    "field,value", [("close_id", "a b"), ("close_id", "a'bad"), ("close_id", "x" * 65), ("revision", "abc")]
)
def test_ordinary_repair_has_safe_placeholders(field, value):
    text = detail(dict(_satisfied(), revision_clean="false", **{field: value}))
    commands = re.findall(r"`(agenttalk [^`]+)`", text)
    assert commands and all(value not in command for command in commands)
    assert "not a ready command" in text


@pytest.mark.parametrize("value", BAD[:-1])
@pytest.mark.parametrize("writer", ["empty", "reopen"])
def test_writers_refuse_non_boolean_without_mutating(value, writer):
    record = _satisfied()
    before = json.dumps(record, sort_keys=True)
    with pytest.raises(close.CloseError, match="revision_clean must be true or false"):
        if writer == "empty":
            close.empty_close(
                "c1",
                scope="release",
                revision=SHA,
                revision_kind="sha",
                gate_scope="release",
                opened_by="lead",
                opened_at="t",
                epoch_at_open=None,
                required_lenses=[],
                revision_clean=value,
                dirty_artifact=None,
            )
        else:
            close.reopen(record, by="lead", at="t", revision_clean=value)
    assert json.dumps(record, sort_keys=True) == before


@pytest.mark.parametrize("value", [True, False])
def test_writer_booleans_and_reopen_none(value):
    record = dict(_satisfied(), revision_clean="damaged")
    assert close.reopen(record, by="lead", at="t", revision_clean=None)["revision_clean"] == "damaged"
    assert close.reopen(record, by="lead", at="t", revision_clean=value)["revision_clean"] is value


def test_empty_writer_refuses_null():
    with pytest.raises(close.CloseError, match="revision_clean must be true or false"):
        close.empty_close(
            "c1",
            scope="release",
            revision=SHA,
            revision_kind="sha",
            gate_scope="release",
            opened_by="lead",
            opened_at="t",
            epoch_at_open=None,
            required_lenses=[],
            revision_clean=None,
            dirty_artifact=None,
        )


@pytest.mark.parametrize("value", [True, False])
def test_empty_writer_stores_boolean_identity(value):
    record = close.empty_close(
        "c1",
        scope="release",
        revision=SHA,
        revision_kind="sha",
        gate_scope="release",
        opened_by="lead",
        opened_at="t",
        epoch_at_open=None,
        required_lenses=[],
        revision_clean=value,
        dirty_artifact=None,
    )
    assert record["revision_clean"] is value


@pytest.mark.parametrize("value,kind", [(0, "number"), (1.0, "number"), ([], "list"), ({}, "object"), (None, "null")])
def test_damage_names_json_type(value, kind):
    assert f"({kind})" in detail(dict(_satisfied(), revision_clean=value))


def test_acceptance_fallback_marks_unsafe_values_and_names_missing_routing():
    record = dict(
        _satisfied(),
        schema_version=close.ACCEPTANCE_SCHEMA_VERSION,
        revision_clean=1,
        acceptance_route={"pending": True},
        scope="bad scope",
        gate_scope="gate;unsafe",
        lane_delivery_artifact="folder/path",
        non_lane_isolation_not_asserted=False,
    )
    record["required_lenses"] = [
        {
            "id": "lens;unsafe",
            "allowed_agents": ["actor space"],
            "allowed_roles": ["role;unsafe"],
            "allowed_groups": ["group"],
            "required": False,
        }
    ]
    text = detail(record)
    command = printed_commands(record)[0]
    assert all(
        value not in command
        for value in ["bad scope", "gate;unsafe", "folder/path", "lens;unsafe", "actor space", "role;unsafe"]
    )
    assert "--optional-lens <lens0>" in command
    assert "--gate-scope <gate_scope>" in command
    assert "--lane-artifact <lane_artifact>" in command
    assert "--allow <lens0>:<authorization-0>" in command
    assert "not a ready command" in text and "group authorizations" in text
    assert "cannot be carried" in text and "\n" not in text


def test_damage_display_survives_non_json_python_value():
    text = detail(dict(_satisfied(), revision_clean=object()))
    assert "a value that cannot be shown" in text


@pytest.mark.parametrize(
    "route,modern,cache",
    [
        ({"schema_version": 2}, True, False),
        ({"schema_version": 3}, True, False),
        ({"schema_version": 4}, True, True),
        ({"schema_version": 1}, False, False),
        ({"pending": True}, False, False),
        ({"schema_version": 7}, False, False),
        ({"schema_version": "2"}, False, False),
        ([], False, False),
    ],
)
@pytest.mark.parametrize("published", [False, True])
def test_acceptance_templates_parse_by_shape(route, modern, cache, published):
    record = dict(
        _satisfied(), schema_version=close.ACCEPTANCE_SCHEMA_VERSION, revision_clean=1, acceptance_route=route
    )
    if published:
        record["status"] = close.PUBLISHED
    text = detail(record)
    assert "cannot be changed in place" in text and "close reopen" not in text
    assert ("close acceptance successor" in text) is modern
    assert ("close publish" in text) is (modern and not published)
    commands = re.findall(r"`(agenttalk [^`]+)`", text)
    assert any("--cache-root <staged-cache-root>" in command for command in commands) is cache
    for command in commands:
        command = re.sub(r"<[^>]+>", "sample", command)
        args = cli.build_parser().parse_args(command.split()[1:])
        assert args.actor == "sample"
        if args.close_cmd == "publish":
            assert args.verdict == "hold"


def printed_commands(record):
    return re.findall(r"`agenttalk ([^`]+)`", detail(record))


def run_printed(case, command):
    values = {
        "lead": "lead",
        "new-id": "next",
        "plan-file": str(case["inputs"] / "plan.json"),
        "project-checkout": str(case["project"]),
        "full-sha": case["sha"],
        "why": "repair",
        "staged-cache-root": str(case.get("stage", {}).get("root", "unused")),
    }
    args = [values[token[1:-1]] if token.startswith("<") else token for token in command.split()]
    return cli.main(["--root", str(case["store"].root), *args])


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize(
    "fixture,published",
    [
        ("case_v2", False),
        ("case_v2", True),
        ("case_v3", False),
        ("candidate", False),
        ("case", False),
        ("pending", False),
    ],
)
def test_printed_acceptance_repair_preserves_parent_and_requirements(
    request, fixture, published, fallback, monkeypatch, capsys
):
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    case = request.getfixturevalue("case" if fixture == "pending" else fixture)
    extra = [
        "--lens",
        "security",
        "--allow",
        "security:runner-a",
        "--optional-lens",
        "docs",
        "--allow",
        "docs:@reviewer",
        "--gate-scope",
        "custom",
    ]
    if fixture == "candidate":
        extra += ["--cache-root", str(case["stage"]["root"])]
    if fixture == "pending":
        with monkeypatch.context() as patch:

            def fail(*args, **kwargs):
                raise close.CloseError("freeze interrupted")

            patch.setattr(acceptance, "freeze", fail)
            assert legacy.open_attempt(case, *extra) == 2
    else:
        assert legacy.open_attempt(case, *extra) == 0
        if fixture == "case_v2":
            legacy.bundle_v2(case)
            assert legacy.attach(case) == 0
        elif fixture == "case_v3":
            legacy.complete_v3(case)
        elif fixture == "candidate":
            data = staging_tests.attachment(case)
            assert legacy.attach(case) == 0
            assert legacy.cold_phase(case, "reconcile") == 0
            legacy.final_accepts(case, data)
    path = close.close_path(case["store"], "attempt")
    record = close.load_close(case["store"], "attempt")
    record["revision_clean"] = 1
    path.write_text(json.dumps(record), encoding="utf-8")
    if published:
        legacy.publish_hold(case)
        record = close.load_close(case["store"], "attempt")
    capsys.readouterr()
    assert legacy.command(case, "check", "--id", "attempt") == 3
    output = capsys.readouterr().out
    commands = re.findall(r"`agenttalk ([^`]+)`", output)
    assert commands
    modern = fixture in {"case_v2", "case_v3", "candidate"}
    if modern:
        for command in commands:
            if command.startswith("close publish"):
                assert run_printed(case, command) == 3
                parent = close.load_close(case["store"], "attempt")
                assert parent["status"] == "published" and parent["final"]["verdict"] == "HOLD"
                assert isinstance(parent["final"]["acceptance_snapshot"], dict)
    before = path.read_bytes()
    if fixture in {"case_v3", "candidate"}:
        legacy.assign_fresh_cold(case, "cold-next")
        if fixture == "candidate":
            staging_tests.save(case)
    selected = next(
        command
        for command in commands
        if command.startswith("close acceptance successor" if modern and not fallback else "close open")
    )
    assert run_printed(case, selected) == 0
    child = close.load_close(case["store"], "next")
    assert child["revision_clean"] is True
    manual = [
        lens
        for lens in record["required_lenses"]
        if not lens["id"].startswith(("acceptance-run-", "acceptance-repro-")) and lens["id"] != "acceptance-cold"
    ]
    assert child["required_lenses"] == acceptance.partition_lenses(case["plan"], manual)
    assert child["gate_scope"] == record["gate_scope"]
    assert child["non_lane_isolation_not_asserted"] is record["non_lane_isolation_not_asserted"]
    assert child["lane_delivery_artifact"] == record["lane_delivery_artifact"]
    assert path.read_bytes() == before
    assert close.HOLD_REVISION not in {h["code"] for h in close.compute_verdict(child, _gate_go())["holds"]}


@pytest.mark.parametrize("published,dirty", [(False, False), (True, False), (False, True)])
def test_printed_ordinary_repair_rereads_checkout(case, monkeypatch, capsys, published, dirty):
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    case["store"] = type(case["store"])(case["project"])
    case["store"].init(["lead"])
    (case["project"] / ".git" / "info" / "exclude").write_text(".agenttalk/\n", encoding="utf-8")
    assert (
        legacy.command(
            case,
            "open",
            "--id",
            "c1",
            "--scope",
            "release",
            "--revision",
            case["sha"],
            "--from",
            "lead",
            "--non-lane-isolation-not-asserted",
        )
        == 0
    )
    assert legacy.command(case, "check", "--id", "c1") == 0
    path = close.close_path(case["store"], "c1")
    record = close.load_close(case["store"], "c1")
    record["revision_clean"] = "false"
    path.write_text(json.dumps(record), encoding="utf-8")
    if published:
        assert legacy.command(case, "publish", "--id", "c1", "--verdict", "hold", "--from", "lead") == 3
        record = close.load_close(case["store"], "c1")
        assert record["status"] == "published" and record["final"]["verdict"] == "HOLD"
    if dirty:
        (case["project"] / "untracked.txt").write_text("work", encoding="utf-8")
    capsys.readouterr()
    assert legacy.command(case, "check", "--id", "c1") == 3
    text = capsys.readouterr().out
    assert "revision_clean" in text
    command = re.findall(r"`agenttalk ([^`]+)`", text)[0]
    assert run_printed(case, command) == 0
    repaired = close.load_close(case["store"], "c1")
    assert repaired["revision_clean"] is (not dirty)
    assert repaired["status"] == "reopened" and repaired["final"] is None
    assert legacy.command(case, "check", "--id", "c1") == (3 if dirty else 0)
    if published:
        assert "publish again" in text
        assert legacy.command(case, "publish", "--id", "c1", "--verdict", "go", "--from", "lead") == 0
        assert close.load_close(case["store"], "c1")["status"] == "published"


def test_text_cleanliness_has_plain_quoted_display():
    text = detail(dict(_satisfied(), revision_clean="false"))
    assert 'revision_clean holds "false" (text)' in text
    assert "\\u0022" not in text


@pytest.mark.parametrize("close_id", ["a b", "a'bad", "x" * 65])
def test_acceptance_successor_marks_unsafe_parent(close_id):
    record = dict(
        _satisfied(),
        revision_clean="false",
        close_id=close_id,
        schema_version=close.ACCEPTANCE_SCHEMA_VERSION,
        acceptance_route={"schema_version": 2},
    )
    text = detail(record)
    successor = next(
        command for command in printed_commands(record) if command.startswith("close acceptance successor")
    )
    assert "--parent <id>" in successor
    assert "--parent None" not in successor
    assert "not a ready command" in text


def test_missing_cleanliness_is_distinct_from_stored_null():
    record = dict(_satisfied(), revision_clean=None)
    null_text = detail(record)
    record.pop("revision_clean")
    missing_text = detail(record)
    assert "revision_clean is missing" in missing_text
    assert 'revision_clean holds "null" (null)' in null_text
    assert missing_text != null_text


@pytest.mark.parametrize("acceptance_attempt", [False, True])
@pytest.mark.parametrize("stored_id", ["a b", "a'bad", "x" * 65])
def test_repair_note_shows_stored_unsafe_id(acceptance_attempt, stored_id):
    record = dict(_satisfied(), revision_clean="false", close_id=stored_id)
    if acceptance_attempt:
        record.update(schema_version=close.ACCEPTANCE_SCHEMA_VERSION, acceptance_route={"schema_version": 2})
    text = detail(record)
    assert f"<id> is {pc.shown(stored_id)}" in text
    assert "NoneType" not in text
    commands = printed_commands(record)
    assert all(stored_id not in command for command in commands)
    assert any("<id>" in command for command in commands)
    assert "not a ready command" in text


@pytest.mark.parametrize("size", [64, 65])
def test_repair_id_length_boundary(size):
    stored_id = "x" * size
    record = dict(_satisfied(), revision_clean=1, close_id=stored_id)
    command = printed_commands(record)[0]
    assert (f"--id {stored_id}" in command) is (size == 64)
    assert ("--id <id>" in command) is (size == 65)


@pytest.mark.parametrize("acceptance_attempt", [False, True])
@pytest.mark.parametrize("stored_id", ["bad id " * 15, "bad'id" * 15, "x" * 65])
def test_overlong_repair_id_is_shown_once_per_note(acceptance_attempt, stored_id):
    record = dict(_satisfied(), revision_clean="false", close_id=stored_id)
    if acceptance_attempt:
        record.update(schema_version=close.ACCEPTANCE_SCHEMA_VERSION, acceptance_route={"schema_version": 2})
    text = detail(record)
    assert text.count(pc.shown(stored_id)) == (2 if acceptance_attempt else 1)
    assert "exceeds 64 characters" in text
    assert all(stored_id not in command for command in printed_commands(record))


@pytest.mark.parametrize("acceptance_attempt", [False, True])
@pytest.mark.parametrize("stored_null", [False, True])
def test_missing_repair_id_says_missing(acceptance_attempt, stored_null):
    record = dict(_satisfied(), revision_clean="false")
    if stored_null:
        record["close_id"] = None
    else:
        record.pop("close_id")
    if acceptance_attempt:
        record.update(schema_version=close.ACCEPTANCE_SCHEMA_VERSION, acceptance_route={"schema_version": 2})
    text = close._revision_clean_damage(record)
    assert text.count("<id> is missing") == (2 if acceptance_attempt else 1)
    assert "NoneType" not in text
    assert "not a ready command" in text
    assert "--id <id>" in text
