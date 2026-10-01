"""wb-planned-lane: `board import-plan`/`retire-plan` and the Planned column they feed."""
import json
from datetime import datetime, timezone

import pytest

from agenttalk import cli, work_board_facts as F
from agenttalk.store import Store
from test_work_board_facts import board
from test_work_board_reducer import BUILDER, LEAD, POLICY, REVIEWER, Bus

PLAN = """# Plan: board lanes and the v2 team views

Plan revision: r2 (supersedes r1) · Status: approved

## 5. Work items

| work_item | Phase | Owner (vendor) | Starts when |
|---|---|---|---|
| wb-done-facts | 1 | dev-2 (claude) | challenge disposed |
| wb-planned-lane | 1 | dev-6 (claude) | wb-done-facts merged |
| wb-lanes-ui | 1 | frontend (claude) | wb-planned-lane merged |
"""

PLAN_ID = "board-lanes-and-the-v2-team-views"

NO_TITLE = """no title here

Plan revision: r1

## 5. Work items

| work_item | Phase | Owner (vendor) | Starts when |
|---|---|---|---|
| a | 1 | x | y |
"""

NO_REV = """# Plan: x

## 5. Work items

| work_item | Phase | Owner (vendor) | Starts when |
|---|---|---|---|
| a | 1 | x | y |
"""

NO_TABLE = """# Plan: x

Plan revision: r1

no table section here
"""

MISSING_COLS = """# Plan: x

Plan revision: r1

## 5. Work items

| Phase | Starts when |
|---|---|
| 1 | y |
"""

DUP_WORK_ITEM = """# Plan: x

Plan revision: r1

## 5. Work items

| work_item | Phase | Owner (vendor) | Starts when |
|---|---|---|---|
| a | 1 | x | y |
| a | 2 | x2 | y2 |
"""

BAD_WORK_ITEM = """# Plan: x

Plan revision: r1

## 5. Work items

| work_item | Phase | Owner (vendor) | Starts when |
|---|---|---|---|
| Not A Slug | 1 | x | y |
"""

EMPTY_TABLE = """# Plan: board lanes and the v2 team views

Plan revision: r3

## 5. Work items

| work_item | Phase | Owner (vendor) | Starts when |
|---|---|---|---|
"""

ESCAPED_PIPE = r"""# Plan: escape test

Plan revision: r1

## 5. Work items

| work_item | Phase | Owner (vendor) | Starts when |
|---|---|---|---|
| task-a | 1 | dev \| backup | ready |
"""

INCOMPLETE_ROW = """# Plan: incomplete test

Plan revision: r1

## 5. Work items

| work_item | Phase | Owner (vendor) | Starts when |
|---|---|---|---|
| task-a | 1 | | ready |
| task-b | 1 | dev | ready |
"""

WRONG_CELL_COUNT = """# Plan: wrong width test

Plan revision: r1

## 5. Work items

| work_item | Phase | Owner (vendor) | Starts when |
|---|---|---|---|
| task-a | 1 | dev |
| task-b | 1 | dev | ready |
"""


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def new_store(tmp_path, agents=(LEAD,)):
    store = Store(tmp_path / "store")
    store.init(list(agents))
    store.set_role(LEAD, "lead")
    return store


# ------------------------------------------------------------------------------- _parse_plan

def test_parse_plan_extracts_id_rev_and_rows():
    plan_id, explicit_id, plan_name, plan_rev, rows, skipped, problems = F._parse_plan(PLAN)
    assert problems == [] and skipped == [] and explicit_id is None
    assert (plan_id, plan_name, plan_rev) == (PLAN_ID, "board lanes and the v2 team views", "r2")
    assert [r["work_item"] for r in rows] == ["wb-done-facts", "wb-planned-lane", "wb-lanes-ui"]
    assert rows[0] == {"work_item": "wb-done-facts", "phase": "1", "owner": "dev-2 (claude)",
                       "starts_when": "challenge disposed"}


@pytest.mark.parametrize("broken,msg", [
    (NO_TITLE, "missing a '# Plan: <name>' title line"),
    (NO_REV, "missing a 'Plan revision: rN' line"),
    (NO_TABLE, "missing a '## 5. Work items' section"),
    (MISSING_COLS, "missing column(s)"),
    (DUP_WORK_ITEM, "duplicate work_item"),
    (BAD_WORK_ITEM, "invalid work_item"),
])
def test_parse_plan_refuses_malformed_input(broken, msg):
    _, _, _, _, rows, skipped, problems = F._parse_plan(broken)
    assert rows == [] and skipped == []
    assert any(msg in p for p in problems)


def test_empty_table_is_structurally_valid_not_a_problem():
    plan_id, explicit_id, plan_name, plan_rev, rows, skipped, problems = F._parse_plan(EMPTY_TABLE)
    assert problems == [] and rows == [] and skipped == []
    assert (plan_id, plan_rev) == (PLAN_ID, "r3")


def test_escaped_pipe_is_literal_cell_content_not_a_delimiter():
    _, _, _, _, rows, skipped, problems = F._parse_plan(ESCAPED_PIPE)
    assert problems == [] and skipped == []
    assert rows == [{"work_item": "task-a", "phase": "1", "owner": "dev | backup", "starts_when": "ready"}]


def test_incomplete_row_is_skipped_the_rest_still_imports():
    _, _, _, _, rows, skipped, problems = F._parse_plan(INCOMPLETE_ROW)
    assert problems == []
    assert [r["work_item"] for r in rows] == ["task-b"]
    assert len(skipped) == 1 and "task-a" in skipped[0][1]


def test_wrong_cell_count_is_skipped_the_rest_still_imports():
    _, _, _, _, rows, skipped, problems = F._parse_plan(WRONG_CELL_COUNT)
    assert problems == []
    assert [r["work_item"] for r in rows] == ["task-b"]
    assert len(skipped) == 1 and "wrong number of cells" in skipped[0][1]


# ------------------------------------------------------------------------------- import_plan

def test_import_plan_writes_its_own_section_and_keeps_integration_untouched(tmp_path):
    store = new_store(tmp_path, [LEAD, BUILDER, REVIEWER])
    plan_file = write(tmp_path / "plan.md", PLAN)
    result = F.import_plan(store, plan_file)
    assert result["plan_id"] == PLAN_ID
    assert len(result["rows"]) == 3 and result["skipped"] == []
    doc = json.loads((store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8"))
    assert doc["schema_version"] == F.SCHEMA_VERSION
    assert doc["planned"]["schema_version"] == F.PLANNED_SCHEMA_VERSION
    assert set(doc["planned"]["plans"]) == {PLAN_ID}
    assert "integration" not in doc


def test_reimport_replaces_this_plans_rows_atomically_others_untouched(tmp_path):
    store = new_store(tmp_path)
    other = PLAN.replace("# Plan: board lanes and the v2 team views", "# Plan: a different initiative"
                         ).replace("Plan revision: r2", "Plan revision: r1")
    F.import_plan(store, write(tmp_path / "other.md", other))
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    shrunk = PLAN.replace("| wb-lanes-ui | 1 | frontend (claude) | wb-planned-lane merged |\n", "")
    F.import_plan(store, write(tmp_path / "plan2.md", shrunk))
    doc = json.loads((store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8"))
    plans = doc["planned"]["plans"]
    assert set(plans) == {PLAN_ID, "a-different-initiative"}
    assert [r["work_item"] for r in plans[PLAN_ID]["rows"]] == ["wb-done-facts", "wb-planned-lane"]
    assert plans["a-different-initiative"]["plan_rev"] == "r1"  # untouched by the re-import above


def test_reimport_with_an_empty_table_clears_the_plans_rows(tmp_path):
    store = new_store(tmp_path)
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    result = F.import_plan(store, write(tmp_path / "plan2.md", EMPTY_TABLE))
    assert result["rows"] == []
    doc = json.loads((store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8"))
    entry = doc["planned"]["plans"][PLAN_ID]
    assert entry["rows"] == [] and entry["plan_rev"] == "r3"


def test_refuses_on_malformed_file_and_publishes_nothing(tmp_path):
    store = new_store(tmp_path)
    bad = write(tmp_path / "bad.md", "not a plan file at all\n")
    with pytest.raises(F.PlanRefused, match="plan file malformed"):
        F.import_plan(store, bad)
    assert not (store.state_dir / F.FACTS_FILE).exists()


def test_cli_import_plan_success_and_malformed_exit_codes(tmp_path, capsys):
    store = new_store(tmp_path)
    plan_file = write(tmp_path / "plan.md", PLAN)
    assert cli.main(["--root", str(store.root), "board", "import-plan", str(plan_file), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["plan_id"] == PLAN_ID and len(out["rows"]) == 3 and out["skipped"] == []
    bad = write(tmp_path / "bad.md", "not a plan\n")
    assert cli.main(["--root", str(store.root), "board", "import-plan", str(bad)]) == 2
    assert "plan file malformed" in capsys.readouterr().err


def test_cli_import_plan_lists_skipped_rows(tmp_path, capsys):
    store = new_store(tmp_path)
    plan_file = write(tmp_path / "plan.md", INCOMPLETE_ROW)
    assert cli.main(["--root", str(store.root), "board", "import-plan", str(plan_file)]) == 0
    out = capsys.readouterr().out
    assert "1 skipped" in out and "skipped" in out and "task-a" in out


# -------------------------------------------------------------------------- plan identity (C)

def test_explicit_plan_id_survives_a_title_change(tmp_path):
    store = new_store(tmp_path)
    v1 = "# Plan: original title\n\nPlan id: stable-id\n\n" + PLAN.split("\n\n", 1)[1]
    v2 = "# Plan: renamed title\n\nPlan id: stable-id\n\n" + PLAN.split("\n\n", 1)[1]
    F.import_plan(store, write(tmp_path / "v1.md", v1))
    result = F.import_plan(store, write(tmp_path / "v2.md", v2))
    assert result["plan_id"] == "stable-id"
    doc = json.loads((store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8"))
    assert set(doc["planned"]["plans"]) == {"stable-id"}
    assert doc["planned"]["plans"]["stable-id"]["plan_name"] == "renamed title"


def test_collision_guard_refuses_a_slug_derived_id_for_a_different_title(tmp_path):
    store = new_store(tmp_path)
    table = PLAN.split("\n\n", 1)[1]
    first = "# Plan: Foo!!\n\n" + table   # slugifies to "foo"
    second = "# Plan: Foo??\n\n" + table  # ALSO slugifies to "foo" - a different plan, same slug
    F.import_plan(store, write(tmp_path / "first.md", first))
    with pytest.raises(F.PlanRefused, match="add an explicit"):
        F.import_plan(store, write(tmp_path / "second.md", second))
    doc = json.loads((store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8"))
    assert doc["planned"]["plans"]["foo"]["plan_name"] == "Foo!!"  # untouched by the refused import


def test_collision_guard_does_not_block_reimporting_the_same_title(tmp_path):
    store = new_store(tmp_path)
    F.import_plan(store, write(tmp_path / "a.md", PLAN))
    result = F.import_plan(store, write(tmp_path / "b.md", PLAN))  # same title, same slug: fine
    assert result["plan_id"] == PLAN_ID


# --------------------------------------------------------------------------------- retire_plan

def test_retire_plan_removes_rows_others_untouched(tmp_path):
    store = new_store(tmp_path)
    other = PLAN.replace("# Plan: board lanes and the v2 team views", "# Plan: another plan"
                         ).replace("Plan revision: r2", "Plan revision: r1")
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    F.import_plan(store, write(tmp_path / "other.md", other))
    F.retire_plan(store, PLAN_ID)
    doc = json.loads((store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8"))
    assert set(doc["planned"]["plans"]) == {"another-plan"}


def test_retire_plan_refuses_and_publishes_nothing_when_absent(tmp_path):
    store = new_store(tmp_path)
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    before = (store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8")
    with pytest.raises(F.PlanRefused, match="no planned rows are recorded"):
        F.retire_plan(store, "no-such-plan")
    assert (store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8") == before


def test_cli_retire_plan(tmp_path, capsys):
    store = new_store(tmp_path)
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    assert cli.main(["--root", str(store.root), "board", "retire-plan", PLAN_ID]) == 0
    assert f"plan {PLAN_ID!r} removed" in capsys.readouterr().out
    assert cli.main(["--root", str(store.root), "board", "retire-plan", PLAN_ID]) == 2
    assert "no planned rows are recorded" in capsys.readouterr().err


# ---------------------------------------------------------------- board feed (dispatch/conflict)

def test_dispatched_item_wins_undispatched_rows_show_planned(tmp_path):
    store = new_store(tmp_path, [LEAD, BUILDER, REVIEWER])
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    bus = Bus()
    build = bus.task("tk-build", BUILDER, "build", item="wb-done-facts", **POLICY)
    bus.reply(build, verdict="done")
    for m in bus.messages:
        (store.messages_dir / f"{m.id}.json").write_text(json.dumps(m.to_dict()), encoding="utf-8")
    items, feed = board(store)
    # dispatched: keeps its OWN derived column, never shown as planned
    assert items["wb-done-facts"]["workflow_column"] != "planned"
    # undispatched plan rows: Planned, carrying the plan's own fields
    for slug in ("wb-planned-lane", "wb-lanes-ui"):
        assert items[slug]["workflow_column"] == "planned"
        assert items[slug]["planned"]["plan_id"] == PLAN_ID
        assert items[slug]["planned"]["plan_rev"] == "r2"
    assert items["wb-planned-lane"]["planned"]["owner"] == "dev-6 (claude)"
    assert not [e for e in feed["errors"] if "planned" in e]


def test_cross_plan_conflict_is_unknown_never_picks_one(tmp_path):
    store = new_store(tmp_path)
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    other = PLAN.replace("# Plan: board lanes and the v2 team views", "# Plan: a rival initiative"
                         ).replace("Plan revision: r2", "Plan revision: r1")
    F.import_plan(store, write(tmp_path / "other.md", other))  # also claims wb-done-facts etc.
    items, feed = board(store)
    assert items["wb-done-facts"]["workflow_column"] == "unknown"
    assert "planned in more than one plan" in items["wb-done-facts"]["reason"]
    assert items["wb-done-facts"]["planned"] is None


def test_malformed_planned_section_is_no_rows_and_a_warning_never_breaks_the_board(tmp_path):
    store = new_store(tmp_path)
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    facts = store.state_dir / F.FACTS_FILE
    doc = json.loads(facts.read_text(encoding="utf-8"))
    doc["planned"]["schema_version"] = 99
    facts.write_text(json.dumps(doc), encoding="utf-8")
    items, feed = board(store)
    assert "wb-planned-lane" not in items
    assert any("planned facts schema unsupported" in e for e in feed["errors"])


def test_planned_rows_are_cut_first_under_the_card_bound(tmp_path):
    store = new_store(tmp_path, [LEAD, BUILDER, REVIEWER])
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    bus = Bus()
    build = bus.task("tk-build", BUILDER, "build", item="wb-done-facts", **POLICY)
    bus.reply(build, verdict="done")
    for m in bus.messages:
        (store.messages_dir / f"{m.id}.json").write_text(json.dumps(m.to_dict()), encoding="utf-8")
    from agenttalk.envelope_snapshot import SnapshotService
    from agenttalk import gates, work_board_feed
    service = SnapshotService(store)
    assert service.refresh()
    snapshot = service.current
    now = datetime.now(timezone.utc)
    evidence = F.load_integration(store, store.load_config(), now=now)
    plans = F.load_planned(store, store.load_config(), now=now)
    feed = work_board_feed.build(snapshot, project=store.project_id(), lead=store.sole_lead(), now=now,
                                 gate_state=gates.load_gate_state(store.root), integration=evidence,
                                 planned=plans, card_limit=1)
    assert [i["work_item"] for i in feed["items"]] == ["wb-done-facts"]
    assert feed["truncated"] is True


# -------------------------------------------------------------- absent integration section (P2 4)

def test_absent_integration_section_is_missing_not_malformed(tmp_path):
    store = new_store(tmp_path)
    cfg = json.loads(store.config_path.read_text(encoding="utf-8"))
    cfg["work_repos"] = {"agenttalk": {"path": str(tmp_path), "targets": ["refs/heads/master"], "default": True}}
    store.config_path.write_text(json.dumps(cfg), encoding="utf-8")
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))  # creates the facts file, "planned" only
    evidence = F.load_integration(store, store.load_config(), now=datetime.now(timezone.utc))
    assert evidence["warnings"] == ["integration evidence missing"]
    assert evidence["facts"] == []
