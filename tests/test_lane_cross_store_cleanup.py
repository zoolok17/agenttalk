"""Two agenttalk stores share one Git repository: a cleanup run in one store must never remove the other store's
checkout.

Lane branches (`lane/<id>`) belong to the whole repository, but each store keeps its own lanes.json. Store B makes lane
`x` for real (`lane assign`, its own clean linked checkout on `lane/x`). Store A also has a row `x`. Every test asserts
the SAFE outcome: whatever store A runs, store B's checkout is still there, still registered, still on `lane/x`.

How store A comes to hold a row `x` that names B's checkout:

* `copied`: B's row copied byte for byte into A's lanes.json (a store folder copied to start a second team's store, or
  a lanes.json backup restored into the wrong store). Every field is genuine, so every check on the row passes.
* `legacy-relative`: the same copy, but the row is the older spelling, a `worktree_path` relative to the store folder
  (`.worktrees/x-...`) with no saved canonical path, and A's command is run from B's folder, where that relative name
  is read. `legacy-relative-with-canonical` keeps the saved absolute canonical path beside the relative name.
* `own-retired` (gc only, no hand edit at all): A made and retired its own lane `x` earlier, through the CLI, and
  deleted the branch; then B reused the id. A's old row still says "abandoned". `own-retired-no-canonical` is the
  same row without its saved canonical path (a hand edit: the field has been saved since lane checkouts began).
* `own-advisory` (gc only, no hand edit at all): A's lane `x` is an advisory lane (`--advisory --no-worktree`), made
  and abandoned through the CLI. It saves no checkout path, so nothing in it can disagree with B's checkout.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from agenttalk import cli, lanes
from agenttalk.store import Store

LANE = "x"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8").stdout


def _domains(store: Store) -> None:
    (store.dir / "domains.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "domains": {"core": {"title": "Core", "owners": {"agents": ["dev"]}, "owned_globs": ["src/core/**"]}},
                "shared_paths": [],
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def two_stores(tmp_path, monkeypatch):
    """Store B at the repository's main folder; store A in a second (detached) linked checkout of the SAME repository,
    initialised like B. Returns (root_a, root_b, base)."""
    from test_lanes import _repo

    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    root_b = tmp_path / "b"
    root_b.mkdir()
    _repo(root_b)
    base = _git(root_b, "rev-parse", "HEAD").strip()
    root_a = tmp_path / "a"
    added = subprocess.run(
        ["git", "-C", str(root_b), "worktree", "add", "--detach", str(root_a), base], capture_output=True, text=True
    )
    assert added.returncode == 0, added.stderr
    store_a = Store(root_a)
    store_a.init(["lead", "dev", "dev2"])
    store_a.set_role("lead", "lead")
    _domains(store_a)
    assert cli._common_git_dir(root_a) == cli._common_git_dir(root_b)  # one repository, two stores
    return root_a, root_b, base, _git(root_b, "branch", "--show-current").strip()


def _assign(root: Path, base: str, target: str) -> dict:
    """The real `lane assign` (as `test_lanes._assign_worktree`), with the target named: store A's folder is a detached
    checkout, which has no current branch to default to."""
    assert (
        cli.main(
            [
                "--root",
                str(root),
                "lane",
                "assign",
                "--id",
                LANE,
                "--from",
                "lead",
                "--assignee",
                "dev",
                "--domain",
                "core",
                "--base",
                base,
                "--target",
                target,
                "--path",
                "src/core",
                "--worktrees-root",
                str(root / ".worktrees"),
            ]
        )
        == 0
    )
    return lanes.load_lanes(Store(root))["lanes"][LANE]


def _b_checkout_is_intact(root_b: Path, checkout: Path) -> None:
    assert checkout.is_dir(), f"store B's checkout {checkout} was removed by a command run in store A"
    listed = _git(root_b, "worktree", "list", "--porcelain")
    assert f"branch refs/heads/lane/{LANE}" in listed
    assert _git(checkout, "symbolic-ref", "--short", "HEAD").strip() == f"lane/{LANE}"
    assert lanes.load_lanes(Store(root_b))["lanes"][LANE]["status"] == lanes.STATUS_ACTIVE


def _row_in_a(root_a: Path, root_b: Path, b_row: dict, route: str, monkeypatch) -> None:
    row = json.loads(json.dumps(b_row))
    if route.startswith("legacy-relative"):
        row["worktree_path"] = os.path.relpath(b_row["worktree_path"], root_b)
        if route == "legacy-relative":
            row.pop("worktree_toplevel_canonical", None)
        monkeypatch.chdir(root_b)  # the folder the person runs store A's command from
    else:
        monkeypatch.chdir(root_a)
    store_a = Store(root_a)
    data = lanes.load_lanes(store_a)
    data.setdefault("lanes", {})[LANE] = row
    lanes.save_lanes(store_a, data)


@pytest.mark.parametrize("route", ["copied", "legacy-relative", "legacy-relative-with-canonical"])
def test_abandon_in_store_a_leaves_store_b_checkout(two_stores, monkeypatch, capsys, route):
    root_a, root_b, base, target = two_stores
    b_row = _assign(root_b, base, target)
    checkout = Path(b_row["worktree_path"])
    _row_in_a(root_a, root_b, b_row, route, monkeypatch)
    capsys.readouterr()

    rc = cli.main(["--root", str(root_a), "lane", "abandon", "--id", LANE])

    out = capsys.readouterr()
    print(f"[abandon/{route}] rc={rc} out={out.out.strip()!r} err={out.err.strip()!r}")
    print(f"[abandon/{route}] A row after: {lanes.load_lanes(Store(root_a))['lanes'][LANE].get('worktree_state')!r}")
    _b_checkout_is_intact(root_b, checkout)


@pytest.mark.parametrize("route", ["copied", "legacy-relative", "legacy-relative-with-canonical"])
def test_delivery_cleanup_in_store_a_leaves_store_b_checkout(two_stores, monkeypatch, capsys, route):
    from test_lanes import _commit

    root_a, root_b, base, target = two_stores
    b_row = _assign(root_b, base, target)
    checkout = Path(b_row["worktree_path"])
    _commit(checkout, "src/core/a.py", "base\nB's work in progress\n")  # B's committed work on lane/x
    _row_in_a(root_a, root_b, b_row, route, monkeypatch)
    capsys.readouterr()

    rc = cli.main(["--root", str(root_a), "lane", "deliver", "--id", LANE, "--from", "dev"])

    out = capsys.readouterr()
    print(f"[deliver/{route}] rc={rc} out={out.out.strip()!r} err={out.err.strip()!r}")
    _b_checkout_is_intact(root_b, checkout)


@pytest.mark.parametrize("route", ["copied", "own-retired", "own-retired-no-canonical", "own-advisory"])
def test_gc_delete_in_store_a_leaves_store_b_checkout(two_stores, monkeypatch, capsys, route):
    root_a, root_b, base, target = two_stores
    store_a = Store(root_a)
    monkeypatch.chdir(root_a)
    if route.startswith("own-retired"):
        # A's own lane x, made and retired through the CLI; its checkout and branch are gone, so B may reuse the id.
        a_row = _assign(root_a, base, target)
        assert cli.main(["--root", str(root_a), "lane", "abandon", "--id", LANE, "--delete-branch"]) == 0
        assert not Path(a_row["worktree_path"]).exists()
        assert not _git(root_a, "branch", "--list", f"lane/{LANE}").strip()
        b_row = _assign(root_b, base, target)
        if route == "own-retired-no-canonical":  # an older row: no canonical path was saved then
            data = lanes.load_lanes(store_a)
            data["lanes"][LANE].pop("worktree_toplevel_canonical", None)
            lanes.save_lanes(store_a, data)
    elif route == "own-advisory":
        assert (
            cli.main(
                [
                    "--root",
                    str(root_a),
                    "lane",
                    "assign",
                    "--id",
                    LANE,
                    "--from",
                    "lead",
                    "--assignee",
                    "dev",
                    "--domain",
                    "core",
                    "--base",
                    base,
                    "--target",
                    target,
                    "--path",
                    "src/core",
                    "--advisory",
                    "--no-worktree",
                    "--worktree-waiver-reason",
                    "coordination only",
                ]
            )
            == 0
        )
        assert cli.main(["--root", str(root_a), "lane", "abandon", "--id", LANE]) == 0
        b_row = _assign(root_b, base, target)
    else:
        b_row = _assign(root_b, base, target)
        row = json.loads(json.dumps(b_row))
        row["status"] = lanes.STATUS_ABANDONED  # a copied row that a person retired in A while B's checkout was busy
        row["worktree_state"] = lanes.STATUS_CLEANUP_PENDING
        lanes.save_lanes(store_a, {"schema_version": 1, "lanes": {LANE: row}})
    checkout = Path(b_row["worktree_path"])
    print(f"[gc/{route}] A row before: {json.dumps(lanes.load_lanes(store_a)['lanes'][LANE], sort_keys=True)}")
    capsys.readouterr()

    rc = cli.main(["--root", str(root_a), "lane", "gc", "--delete", "--json"])

    out = capsys.readouterr()
    print(f"[gc/{route}] rc={rc} out={out.out.strip()!r} err={out.err.strip()!r}")
    _b_checkout_is_intact(root_b, checkout)
    assert _git(root_b, "branch", "--list", f"lane/{LANE}").strip()


# ---------------------------------------------------------------------------------------------------------------------
# Added in round 22 (the file above is the cross-store reproduction, unchanged): what store A still does for its OWN
# lanes while store B holds a lane of the same repository, and that a delivery for B's checkout writes nothing.
# ---------------------------------------------------------------------------------------------------------------------


def _assign_in(root: Path, lane_id: str, base: str, target: str) -> dict:
    assert (
        cli.main(
            [
                "--root", str(root), "lane", "assign", "--id", lane_id, "--from", "lead", "--assignee", "dev",
                "--domain", "core", "--base", base, "--target", target, "--path", "src/core",
                "--worktrees-root", str(root / ".worktrees"),
            ]
        )
        == 0
    )
    return lanes.load_lanes(Store(root))["lanes"][lane_id]


def test_deliver_in_store_a_for_store_b_checkout_writes_nothing(two_stores, monkeypatch, capsys):
    """Refused before anything is written: no committed final, no pending delivery, the row still active."""
    from test_lanes import _commit

    root_a, root_b, base, target = two_stores
    b_row = _assign(root_b, base, target)
    checkout = Path(b_row["worktree_path"])
    _commit(checkout, "src/core/a.py", "base\nB's work in progress\n")
    _row_in_a(root_a, root_b, b_row, "copied", monkeypatch)
    store_a = Store(root_a)
    capsys.readouterr()

    assert cli.main(["--root", str(root_a), "lane", "deliver", "--id", LANE, "--from", "dev"]) != 0

    deliveries = store_a.dir / lanes.DELIVERIES_DIRNAME
    assert not deliveries.exists() or not [p for p in deliveries.rglob("*") if p.is_file()]
    row = lanes.load_lanes(store_a)["lanes"][LANE]
    assert row["status"] == lanes.STATUS_ACTIVE and "publish_pending" not in row
    _b_checkout_is_intact(root_b, checkout)


def test_store_a_still_removes_its_own_lanes_checkout_beside_store_b_lane(two_stores, monkeypatch, capsys):
    """The control: abandon and gc in A take A's own lane `y` (its checkout is inside A's folder), and B's `x` stays."""
    root_a, root_b, base, target = two_stores
    b_row = _assign(root_b, base, target)
    own = Path(_assign_in(root_a, "y", base, target)["worktree_path"])
    assert own.is_dir() and root_a in own.parents
    monkeypatch.chdir(root_a)
    capsys.readouterr()

    assert cli.main(["--root", str(root_a), "lane", "abandon", "--id", "y"]) == 0
    assert not own.exists()
    assert cli.main(["--root", str(root_a), "lane", "gc", "--delete", "--json"]) == 0

    _b_checkout_is_intact(root_b, Path(b_row["worktree_path"]))


def test_store_a_still_delivers_its_own_lane_and_removes_its_checkout(two_stores, monkeypatch, capsys):
    from test_lanes import _commit

    root_a, root_b, base, target = two_stores
    b_row = _assign(root_b, base, target)
    own = Path(_assign_in(root_a, "y", base, target)["worktree_path"])
    _commit(own, "src/core/a.py", "base\nA's own work\n")
    monkeypatch.chdir(root_a)
    capsys.readouterr()

    assert cli.main(["--root", str(root_a), "lane", "deliver", "--id", "y", "--from", "dev"]) == 0

    assert not own.exists()
    assert lanes.load_lanes(Store(root_a))["lanes"]["y"]["status"] == lanes.STATUS_DELIVERED
    _b_checkout_is_intact(root_b, Path(b_row["worktree_path"]))


def test_gc_in_store_a_says_why_store_b_checkout_is_left_alone(two_stores, monkeypatch, capsys):
    root_a, root_b, base, target = two_stores
    b_row = _assign(root_b, base, target)
    _row_in_a(root_a, root_b, b_row, "copied", monkeypatch)
    data = lanes.load_lanes(Store(root_a))
    data["lanes"][LANE]["status"] = lanes.STATUS_ABANDONED
    data["lanes"][LANE]["worktree_state"] = lanes.STATUS_CLEANUP_PENDING
    lanes.save_lanes(Store(root_a), data)
    capsys.readouterr()

    assert cli.main(["--root", str(root_a), "lane", "gc", "--delete", "--json"]) == 0

    item = next(row for row in json.loads(capsys.readouterr().out)["items"] if row["lane_id"] == LANE)
    assert item["worktree"] is None and item["worktree_remove_safe"] is False
    assert "did not make" in item["worktree_reason"] and item["branch_delete_safe"] is False
    _b_checkout_is_intact(root_b, Path(b_row["worktree_path"]))


def test_a_checkout_made_by_a_store_nested_inside_this_one_is_not_this_stores(tmp_path, monkeypatch, capsys):
    """Inside the folder is not enough when another store sits between: that store made it."""
    from test_lanes import _repo

    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)
    outer = tmp_path / "outer"
    outer.mkdir()
    _repo(outer)
    base = _git(outer, "rev-parse", "HEAD").strip()
    target = _git(outer, "branch", "--show-current").strip()
    nested = outer / "nested-team"
    added = subprocess.run(
        ["git", "-C", str(outer), "worktree", "add", "--detach", str(nested), base], capture_output=True, text=True
    )
    assert added.returncode == 0, added.stderr
    inner = Store(nested)
    inner.init(["lead", "dev", "dev2"])
    inner.set_role("lead", "lead")
    _domains(inner)
    n_row = _assign(nested, base, target)
    checkout = Path(n_row["worktree_path"])
    assert outer in checkout.parents
    _row_in_a(outer, nested, n_row, "copied", monkeypatch)
    capsys.readouterr()

    cli.main(["--root", str(outer), "lane", "abandon", "--id", LANE])

    _b_checkout_is_intact(nested, checkout)
