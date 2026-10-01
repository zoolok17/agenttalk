"""wb-planned-lane: `board import-plan`/`retire-plan` and the Planned column they feed."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenttalk import cli, work_board_facts as F
from agenttalk.store import Store
from test_work_board_facts import board
from test_work_board_reducer import BUILDER, LEAD, POLICY, REVIEWER, Bus

# The lead's real, live plan file (copied verbatim) - FIX round 2's P1 regression: a real
# 8-column table, with "Starts when" fifth, imported zero rows and erased prior cards.
REAL_PLAN_PATH = Path(__file__).parent / "fixtures" / "plan-board-lanes-v2-views.md"
REAL_PLAN_WORK_ITEMS = ["wb-done-facts", "wb-planned-lane", "wb-lanes-ui", "v2-team",
                       "v2-history", "v2-learning", "v2-walkthrough"]

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


def test_real_eight_column_plan_imports_exactly_its_seven_rows():
    # FIX round 2 P1: width used to be max(required-column index)+1, not the header's own
    # width, so a real table with extra columns (Reviewer, Estimate, ...) after the four
    # required ones rejected every row as a cell-count mismatch.
    text = REAL_PLAN_PATH.read_text(encoding="utf-8")
    _, _, _, _, rows, skipped, problems = F._parse_plan(text)
    assert problems == [] and skipped == []
    assert [r["work_item"] for r in rows] == REAL_PLAN_WORK_ITEMS


def test_a_prose_line_between_the_heading_and_the_table_is_skipped():
    # The real plan file's own template has one sentence ("Dispatches carry ...") between the
    # "## 5. Work items" heading and the table itself - the parser must look PAST prose to find
    # where the table starts, not require the table to be the literal first line.
    text = ("# Plan: x\n\nPlan revision: r1\n\n## 5. Work items\n"
           "Dispatches carry some prose that is not a table row.\n\n"
           "| work_item | Phase | Owner (vendor) | Starts when |\n|---|---|---|---|\n"
           "| a | 1 | x | y |\n")
    _, _, _, _, rows, skipped, problems = F._parse_plan(text)
    assert problems == [] and skipped == []
    assert [r["work_item"] for r in rows] == ["a"]


def test_a_second_later_table_in_section_5_is_never_read():
    # Recast principle A: parse exactly the FIRST contiguous table. A "Legend:" table further
    # down section 5 must never become bogus Planned cards (reviewer-found).
    text = ("# Plan: x\n\nPlan revision: r1\n\n## 5. Work items\n\n"
           "| work_item | Phase | Owner (vendor) | Starts when |\n|---|---|---|---|\n"
           "| task-a | 1 | dev | ready |\n"
           "\nLegend:\n\n| code | phase | owner | meaning |\n|---|---|---|---|\n"
           "| note | x | y | z |\n")
    _, _, _, _, rows, skipped, problems = F._parse_plan(text)
    assert problems == [] and skipped == []
    assert [r["work_item"] for r in rows] == ["task-a"]


def test_header_only_table_with_no_separator_refuses():
    # FIX round 2 P1/F3: a header line with no separator line at all used to parse as a
    # valid, empty table (zero data rows) instead of a malformed one.
    broken = NO_TITLE.replace("no title here", "# Plan: x").replace("\n|---|---|---|---|\n", "\n")
    _, _, _, _, rows, skipped, problems = F._parse_plan(broken)
    assert rows == [] and skipped == []
    assert any("missing a '## 5. Work items' section" in p for p in problems)


@pytest.mark.parametrize("order", ["incomplete-first", "complete-first"])
def test_duplicate_detection_is_order_independent(order):
    incomplete = "| task-a | 1 | | later |\n"
    complete = "| task-a | 1 | dev | ready |\n"
    rows_text = incomplete + complete if order == "incomplete-first" else complete + incomplete
    header = ("# Plan: dup order test\n\nPlan revision: r1\n\n## 5. Work items\n\n"
             "| work_item | Phase | Owner (vendor) | Starts when |\n|---|---|---|---|\n")
    _, _, _, _, rows, skipped, problems = F._parse_plan(header + rows_text)
    assert rows == [] and skipped == []
    assert any("duplicate work_item" in p for p in problems)


def test_every_row_skipped_refuses_never_silently_clears():
    # FIX round 2 P1: "everything skipped" must never be treated the same as a
    # structurally empty table - only a table with zero DATA ROWS at all may clear a plan.
    all_incomplete = ("# Plan: x\n\nPlan revision: r1\n\n## 5. Work items\n\n"
                      "| work_item | Phase | Owner (vendor) | Starts when |\n|---|---|---|---|\n"
                      "| a | 1 | | y |\n| b | 1 | x | |\n")
    _, _, _, _, rows, skipped, problems = F._parse_plan(all_incomplete)
    assert rows == []
    assert any("every row in section 5 was skipped" in p for p in problems)


def test_malformed_explicit_plan_id_refuses_never_falls_back_to_title():
    # FIX round 2 F2: presence of a "Plan id:" line is detected separately from its
    # validity - a blank or multi-token value must refuse, never silently fall back.
    for bad in ("Plan id: stable id", "Plan id: "):
        text = PLAN.replace("Plan revision: r2", f"{bad}\n\nPlan revision: r2")
        plan_id, explicit_id, _, _, rows, skipped, problems = F._parse_plan(text)
        assert explicit_id is None and rows == [] and skipped == []
        assert any("Plan id:" in p for p in problems), problems


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


def test_import_plan_with_the_real_plan_file_does_not_erase_prior_rows(tmp_path):
    # FIX round 2 P1, end to end: seed a plan under the real plan's own id, then import the
    # real file over it - it must REPLACE with its 7 real rows, never erase down to zero.
    store = new_store(tmp_path)
    real_id = F._parse_plan(REAL_PLAN_PATH.read_text(encoding="utf-8"))[0]
    seed = PLAN.replace("Plan revision: r2", f"Plan id: {real_id}\n\nPlan revision: r2")
    F.import_plan(store, write(tmp_path / "seed.md", seed))
    result = F.import_plan(store, REAL_PLAN_PATH)
    assert [r["work_item"] for r in result["rows"]] == REAL_PLAN_WORK_ITEMS
    assert result["skipped"] == []
    doc = json.loads((store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8"))
    assert [r["work_item"] for r in doc["planned"]["plans"][real_id]["rows"]] == REAL_PLAN_WORK_ITEMS


def test_header_only_import_refuses_and_keeps_stored_rows(tmp_path):
    store = new_store(tmp_path)
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    broken = PLAN.replace("|---|---|---|---|\n", "")
    with pytest.raises(F.PlanRefused):
        F.import_plan(store, write(tmp_path / "plan2.md", broken))
    doc = json.loads((store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8"))
    assert len(doc["planned"]["plans"][PLAN_ID]["rows"]) == 3  # untouched by the refused import


def test_all_rows_skipped_refuses_and_keeps_stored_rows(tmp_path):
    store = new_store(tmp_path)
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    all_incomplete = PLAN.replace("| wb-done-facts | 1 | dev-2 (claude) | challenge disposed |",
                                  "| wb-done-facts | 1 | | challenge disposed |"
                                  ).replace("| wb-planned-lane | 1 | dev-6 (claude) | wb-done-facts merged |",
                                  "| wb-planned-lane | 1 | | wb-done-facts merged |"
                                  ).replace("| wb-lanes-ui | 1 | frontend (claude) | wb-planned-lane merged |",
                                  "| wb-lanes-ui | 1 | | wb-planned-lane merged |")
    with pytest.raises(F.PlanRefused, match="every row in section 5 was skipped"):
        F.import_plan(store, write(tmp_path / "plan2.md", all_incomplete))
    doc = json.loads((store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8"))
    assert len(doc["planned"]["plans"][PLAN_ID]["rows"]) == 3  # untouched by the refused import


def test_one_malformed_stored_plan_is_isolated_others_still_load(tmp_path):
    store = new_store(tmp_path)
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    facts = store.state_dir / F.FACTS_FILE
    doc = json.loads(facts.read_text(encoding="utf-8"))
    corrupt = json.loads(json.dumps(doc["planned"]["plans"][PLAN_ID]))
    corrupt["rows"][0]["work_item"] = 7  # JSON allows any type; the real fault that hid every plan
    doc["planned"]["plans"]["corrupt"] = corrupt
    facts.write_text(json.dumps(doc), encoding="utf-8")
    loaded = F.load_planned(store, store.load_config())
    assert "corrupt" not in loaded["plans"]
    assert PLAN_ID in loaded["plans"]
    assert any("corrupt" in w for w in loaded["warnings"])


# ------------------------------------------------------- recast B: fail closed on stored shape

def test_import_refuses_when_the_stored_plans_container_is_not_a_map(tmp_path):
    # Recast principle B: a "plans" container that is a LIST (not a map) must refuse the write,
    # never be silently treated as empty - that would let the next import overwrite it for good
    # (reviewer-found data loss).
    store = new_store(tmp_path)
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    facts = store.state_dir / F.FACTS_FILE
    doc = json.loads(facts.read_text(encoding="utf-8"))
    doc["planned"]["plans"] = [doc["planned"]["plans"]]
    facts.write_text(json.dumps(doc), encoding="utf-8")
    corrupted = facts.read_bytes()
    with pytest.raises(F.PlanRefused, match="not in the expected shape"):
        F.import_plan(store, write(tmp_path / "plan2.md", PLAN))
    assert facts.read_bytes() == corrupted  # untouched by the refused import


def test_retire_refuses_when_a_stored_entry_has_the_wrong_keys(tmp_path):
    store = new_store(tmp_path)
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    facts = store.state_dir / F.FACTS_FILE
    doc = json.loads(facts.read_text(encoding="utf-8"))
    doc["planned"]["plans"][PLAN_ID]["extra_unexpected_key"] = "x"
    facts.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(F.PlanRefused, match="not in the expected shape"):
        F.retire_plan(store, PLAN_ID)


# -------------------------------------------------------------- recast D: bounded output

def test_thousands_of_malformed_plans_give_a_capped_warning_list_not_an_unbounded_one(tmp_path):
    store = new_store(tmp_path)
    facts = store.state_dir / F.FACTS_FILE
    facts.parent.mkdir(parents=True, exist_ok=True)
    doc = {"schema_version": 1,
          "planned": {"schema_version": 1, "plans": {f"p{i:05}": None for i in range(6000)}}}
    facts.write_text(json.dumps(doc), encoding="utf-8")
    loaded = F.load_planned(store, store.load_config())
    assert loaded["plans"] == {}
    assert len(loaded["warnings"]) == F._MAX_PLANNED_WARNINGS + 1
    assert "and " in loaded["warnings"][-1] and "more" in loaded["warnings"][-1]


def test_bounded_warnings_never_push_active_cards_out_of_the_feed(tmp_path):
    # The reviewer's own end-to-end reproduction: thousands of planned warnings used to grow
    # feed["errors"] large enough that bounded()'s byte-trim popped every real card before ever
    # touching errors. With the cap, the real card survives.
    store = new_store(tmp_path, [LEAD, BUILDER, REVIEWER])
    facts = store.state_dir / F.FACTS_FILE
    doc = {"schema_version": 1,
          "planned": {"schema_version": 1, "plans": {f"p{i:05}": None for i in range(6000)}}}
    facts.write_text(json.dumps(doc), encoding="utf-8")
    bus = Bus()
    build = bus.task("tk-build", BUILDER, "build", item="wb-done-facts", **POLICY)
    bus.reply(build, verdict="done")
    read = bus.task("tk-read", REVIEWER, "read", item="wb-done-facts", work_head="a" * 40)
    bus.reply(read, verdict="GO")
    for m in bus.messages:
        (store.messages_dir / f"{m.id}.json").write_text(json.dumps(m.to_dict()), encoding="utf-8")
    items, feed = board(store)
    assert "wb-done-facts" in items  # the real, active card survives
    assert any("planned section" in e or "malformed" in e for e in feed["errors"])


def test_import_and_retire_tag_the_current_session(tmp_path):
    store = new_store(tmp_path)
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    doc = json.loads((store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8"))
    assert doc["planned"]["session_id"] == store.load_config()["session_id"]


def test_import_refuses_if_the_session_changes_before_the_internal_read(tmp_path, monkeypatch):
    # Recast principle C: project/session are now captured ONCE at the very start of
    # import_plan, before ANY read - so a session change at ANY point after that, including
    # before _write_section's own internal _read, is caught by the fresh re-check immediately
    # before the atomic write. (Earlier this round, capturing session_id inside build() meant a
    # reset before build() ran went undetected - exactly the reviewer's "F4 still open" finding.)
    store = new_store(tmp_path)
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    real_load_config, calls = store.load_config, {"n": 0}

    def flaky_load_config():
        calls["n"] += 1
        cfg = dict(real_load_config())
        if calls["n"] == 1:  # import_plan's OWN capture, at its very first line
            cfg["session_id"] = "stale-session"
        return cfg  # every later read (inside build(), and _write_section's own check) is real

    monkeypatch.setattr(store, "load_config", flaky_load_config)
    with pytest.raises(F.PlanRefused, match="store session changed"):
        F.import_plan(store, write(tmp_path / "plan2.md", PLAN))
    assert PLAN_ID in F.load_planned(store, store.load_config())["plans"]  # the first import stands


@pytest.mark.parametrize("operation", ["import", "retire"])
def test_refuses_across_a_real_reset_after_the_internal_read(tmp_path, operation):
    # The reviewer's own exact interleave (probe_round2.py): a REAL Store.reset(), on an
    # independent thread, synchronously waited, landing right after _write_section's own
    # _read(path) returns (which is itself AFTER import_plan/retire_plan's session capture) but
    # before build() runs. Without principle C this resurrected the pre-reset plan's rows into
    # the new session; with it, the stale capture no longer matches the fresh re-check and the
    # write is refused - nothing is resurrected.
    from concurrent.futures import ThreadPoolExecutor

    from agenttalk.store import Store

    store = new_store(tmp_path)
    F.import_plan(store, write(tmp_path / "plan.md", PLAN))
    other = PLAN.replace("# Plan: board lanes and the v2 team views", "# Plan: another plan"
                         ).replace("Plan revision: r2", "Plan revision: r1")
    F.import_plan(store, write(tmp_path / "other.md", other))
    before_session = store.load_config()["session_id"]
    original_read = F._read

    def reset_after_read(path):
        result = original_read(path)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(Store(store.root).reset).result(10)
        assert store.load_config()["session_id"] != before_session
        return result

    original_read_ref = F._read
    F._read = reset_after_read
    try:
        if operation == "import":
            new_plan = PLAN.replace("# Plan: board lanes and the v2 team views", "# Plan: a new plan"
                                    ).replace("Plan revision: r2", "Plan revision: r3")
            with pytest.raises((F.PlanRefused, OSError)):
                F.import_plan(store, write(tmp_path / "new.md", new_plan))
        else:
            with pytest.raises((F.PlanRefused, OSError)):
                F.retire_plan(store, "another-plan")
    finally:
        F._read = original_read_ref
    retained = F.load_planned(store, store.load_config())["plans"]
    assert "another-plan" not in retained  # not resurrected into the new session
    assert "a-new-plan" not in retained  # the new import was refused, never published


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
    assert any("planned section invalid" in e for e in feed["errors"])


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
