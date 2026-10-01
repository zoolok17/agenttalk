"""wb-planned-lane: `board import-plan` and the Planned column it feeds."""
import json

import pytest

from agenttalk import cli, work_board_facts as F
from agenttalk.store import Store
from test_work_board_facts import board
from test_work_board_reducer import BUILDER, LEAD, POLICY, REVIEWER, Bus

PLAN = """# Plan: board lanes and the v2 team views

Plan revision: r2 (supersedes r1) · Status: approved

## 5. Work items

| Phase | work_item | Owner (vendor) | Reviewer (other vendor) | Starts when | Completion evidence | Estimate | Stopping rule |
|---|---|---|---|---|---|---|---|
| 1 | wb-done-facts | dev-2 (claude) | reviewer-1 (codex) | challenge disposed | evidence | 5-8 h | 2 rounds |
| 1 | wb-planned-lane | dev-6 (claude) | reviewer-4 (codex) | wb-done-facts merged | evidence | 3-5 h | 2 rounds |
| 1 | wb-lanes-ui | frontend (claude) | reviewer-4 (codex) | wb-planned-lane merged | evidence | 2-3 h | 2 rounds |
"""

PLAN_ID = "board-lanes-and-the-v2-team-views"


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_plan_extracts_id_rev_and_rows():
    plan_id, plan_name, plan_rev, rows, problems = F._parse_plan(PLAN)
    assert problems == []
    assert (plan_id, plan_name, plan_rev) == (PLAN_ID, "board lanes and the v2 team views", "r2")
    assert [r["work_item"] for r in rows] == ["wb-done-facts", "wb-planned-lane", "wb-lanes-ui"]
    assert rows[0] == {"work_item": "wb-done-facts", "phase": "1", "owner": "dev-2 (claude)",
                       "starts_when": "challenge disposed"}


@pytest.mark.parametrize("broken,msg", [
    ("no title here\n\nPlan revision: r1\n\n## 5. Work items\n\n| work_item | Phase | Owner (vendor) | Starts when |\n|---|---|---|---|\n| a | 1 | x | y |\n",
     "missing a '# Plan: <name>' title line"),
    ("# Plan: x\n\n## 5. Work items\n\n| work_item | Phase | Owner (vendor) | Starts when |\n|---|---|---|---|\n| a | 1 | x | y |\n",
     "missing a 'Plan revision: rN' line"),
    ("# Plan: x\n\nPlan revision: r1\n\nno table section here\n", "missing a '## 5. Work items' section"),
    ("# Plan: x\n\nPlan revision: r1\n\n## 5. Work items\n\n| Phase | Starts when |\n|---|---|\n| 1 | y |\n",
     "missing column(s)"),
    ("# Plan: x\n\nPlan revision: r1\n\n## 5. Work items\n\n"
     "| work_item | Phase | Owner (vendor) | Starts when |\n|---|---|---|---|\n"
     "| a | 1 | x | y |\n| a | 2 | x2 | y2 |\n", "duplicate work_item"),
    ("# Plan: x\n\nPlan revision: r1\n\n## 5. Work items\n\n"
     "| work_item | Phase | Owner (vendor) | Starts when |\n|---|---|---|---|\n"
     "| Not A Slug | 1 | x | y |\n", "invalid work_item"),
])
def test_parse_plan_refuses_malformed_input(broken, msg):
    _, _, _, rows, problems = F._parse_plan(broken)
    assert rows == []
    assert any(msg in p for p in problems)


def test_import_plan_writes_its_own_section_and_keeps_integration_untouched(tmp_path):
    store = Store(tmp_path / "store")
    store.init([LEAD, BUILDER, REVIEWER])
    store.set_role(LEAD, "lead")
    plan_file = write(tmp_path / "plan.md", PLAN)
    result = F.import_plan(store, plan_file)
    assert result["plan_id"] == PLAN_ID
    assert len(result["rows"]) == 3
    doc = json.loads((store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8"))
    assert doc["schema_version"] == F.SCHEMA_VERSION
    assert doc["planned"]["schema_version"] == F.PLANNED_SCHEMA_VERSION
    assert set(doc["planned"]["plans"]) == {PLAN_ID}
    assert "integration" not in doc


def test_reimport_replaces_this_plans_rows_atomically_others_untouched(tmp_path):
    store = Store(tmp_path / "store")
    store.init([LEAD])
    store.set_role(LEAD, "lead")
    other = PLAN.replace("# Plan: board lanes and the v2 team views", "# Plan: a different initiative"
                         ).replace("Plan revision: r2", "Plan revision: r1")
    F.import_plan(store, write(tmp_path / "other.md", other))
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    shrunk = PLAN.replace("| 1 | wb-lanes-ui | frontend (claude) | reviewer-4 (codex) | "
                          "wb-planned-lane merged | evidence | 2-3 h | 2 rounds |\n", "")
    F.import_plan(store, write(tmp_path / "plan2.md", shrunk))
    doc = json.loads((store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8"))
    plans = doc["planned"]["plans"]
    assert set(plans) == {PLAN_ID, "a-different-initiative"}
    assert [r["work_item"] for r in plans[PLAN_ID]["rows"]] == ["wb-done-facts", "wb-planned-lane"]
    assert plans["a-different-initiative"]["plan_rev"] == "r1"  # untouched by the re-import above


def test_refuses_on_malformed_file_and_publishes_nothing(tmp_path):
    store = Store(tmp_path / "store")
    store.init([LEAD])
    store.set_role(LEAD, "lead")
    bad = write(tmp_path / "bad.md", "not a plan file at all\n")
    with pytest.raises(F.PlanRefused, match="plan file malformed"):
        F.import_plan(store, bad)
    assert not (store.state_dir / F.FACTS_FILE).exists()


def test_cli_import_plan_success_and_malformed_exit_codes(tmp_path, capsys):
    store = Store(tmp_path / "store")
    store.init([LEAD])
    store.set_role(LEAD, "lead")
    plan_file = write(tmp_path / "plan.md", PLAN)
    assert cli.main(["--root", str(store.root), "board", "import-plan", str(plan_file), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["plan_id"] == PLAN_ID and len(out["rows"]) == 3
    bad = write(tmp_path / "bad.md", "not a plan\n")
    assert cli.main(["--root", str(store.root), "board", "import-plan", str(bad)]) == 2
    assert "plan file malformed" in capsys.readouterr().err


def test_dispatched_item_wins_undispatched_rows_show_planned(tmp_path):
    store = Store(tmp_path / "store")
    store.init([LEAD, BUILDER, REVIEWER])
    store.set_role(LEAD, "lead")
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
    store = Store(tmp_path / "store")
    store.init([LEAD])
    store.set_role(LEAD, "lead")
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    other = PLAN.replace("# Plan: board lanes and the v2 team views", "# Plan: a rival initiative"
                         ).replace("Plan revision: r2", "Plan revision: r1")
    F.import_plan(store, write(tmp_path / "other.md", other))  # also claims wb-done-facts etc.
    items, feed = board(store)
    assert items["wb-done-facts"]["workflow_column"] == "unknown"
    assert "planned in more than one plan" in items["wb-done-facts"]["reason"]
    assert items["wb-done-facts"]["planned"] is None


def test_malformed_planned_section_is_no_rows_and_a_warning_never_breaks_the_board(tmp_path):
    store = Store(tmp_path / "store")
    store.init([LEAD])
    store.set_role(LEAD, "lead")
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    facts = store.state_dir / F.FACTS_FILE
    doc = json.loads(facts.read_text(encoding="utf-8"))
    doc["planned"]["schema_version"] = 99
    facts.write_text(json.dumps(doc), encoding="utf-8")
    items, feed = board(store)
    assert "wb-planned-lane" not in items
    assert any("planned facts schema unsupported" in e for e in feed["errors"])


def test_planned_rows_are_cut_first_under_the_card_bound(tmp_path):
    store = Store(tmp_path / "store")
    store.init([LEAD, BUILDER, REVIEWER])
    store.set_role(LEAD, "lead")
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    bus = Bus()
    build = bus.task("tk-build", BUILDER, "build", item="wb-done-facts", **POLICY)
    bus.reply(build, verdict="done")
    for m in bus.messages:
        (store.messages_dir / f"{m.id}.json").write_text(json.dumps(m.to_dict()), encoding="utf-8")
    from agenttalk.envelope_snapshot import SnapshotService
    from agenttalk import gates, work_board_feed
    from datetime import datetime, timezone
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
