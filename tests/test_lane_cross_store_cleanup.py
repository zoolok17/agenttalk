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


def test_the_refusal_for_store_b_checkout_says_it_is_outside_not_that_it_could_not_be_examined(
        two_stores, monkeypatch, capsys):
    """Round 29: a checkout PROVEN to be outside this store is still refused at the delivery boundary, before anything
    is written, and for that reason (not the reason given for a checkout that could not be looked at)."""
    from test_lanes import _commit

    root_a, root_b, base, target = two_stores
    b_row = _assign(root_b, base, target)
    checkout = Path(b_row["worktree_path"])
    _commit(checkout, "src/core/a.py", "base\nB's work in progress\n")
    _row_in_a(root_a, root_b, b_row, "copied", monkeypatch)
    store_a = Store(root_a)
    capsys.readouterr()

    assert cli.main(["--root", str(root_a), "lane", "deliver", "--id", LANE, "--from", "dev"]) == 3

    err = capsys.readouterr().err
    assert "is not inside this store's folder" in err and "could not be examined" not in err
    deliveries = store_a.dir / lanes.DELIVERIES_DIRNAME
    assert not deliveries.exists() or not [p for p in deliveries.rglob("*") if p.is_file()]
    assert lanes.load_lanes(store_a)["lanes"][LANE]["status"] == lanes.STATUS_ACTIVE
    _b_checkout_is_intact(root_b, checkout)


@pytest.mark.parametrize("armed", [None, "_lane_finalize_delivery"], ids=["whole-command", "final-steps-only"])
def test_store_a_never_publishes_store_b_checkout_it_could_not_examine(two_stores, monkeypatch, capsys, armed):
    """Round 30 (codex-rev's probe): store B's lane row is copied into store A, B's work is committed in B's checkout,
    and looking at that checkout fails. Whether the fault is there from the start or only from the final steps on,
    A's `lane deliver` writes nothing: not a committed final, not a pending delivery, the row stays active, and B's
    checkout is untouched. (From the start, the fresh evaluation cannot show the checkout is A's; from the final steps
    on, the fresh evaluation has already seen that it is not.)"""
    from test_lanes import _checkout_cannot_be_resolved, _commit

    root_a, root_b, base, target = two_stores
    b_row = _assign(root_b, base, target)
    checkout = Path(b_row["worktree_path"])
    _commit(checkout, "src/core/a.py", "base\nother store work\n")
    _row_in_a(root_a, root_b, b_row, "copied", monkeypatch)
    store_a = Store(root_a)
    fired = _checkout_cannot_be_resolved(monkeypatch, checkout, only_during=armed)
    capsys.readouterr()

    rc = cli.main(["--root", str(root_a), "lane", "deliver", "--id", LANE, "--from", "dev"])

    err = capsys.readouterr().err
    assert rc == 3 and "was left alone" in err
    assert ("could not be examined" in err) if armed is None else ("is not inside this store's folder" in err)
    deliveries = store_a.dir / lanes.DELIVERIES_DIRNAME
    assert not deliveries.exists() or not [p for p in deliveries.rglob("*") if p.is_file()]
    row = lanes.load_lanes(store_a)["lanes"][LANE]
    assert row["status"] == lanes.STATUS_ACTIVE and "publish_pending" not in row and "delivery_artifact" not in row
    _b_checkout_is_intact(root_b, checkout)
    assert fired or armed is not None


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


# ---------------------------------------------------------------------------------------------------------------------
# Round 25 (zteam's challenger): gc pairs a lane branch with a checkout of that branch from Git's repository-wide list.
# With one `lane/x` checked out in EACH store (Git allows it with --force), the pairing must be this store's own,
# whichever one Git happens to list last.
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("b_listed", ["last", "first"])
@pytest.mark.parametrize("managed", [True, False], ids=["under-a-managed-root", "plain-folder-in-a"])
@pytest.mark.parametrize("a_row", ["advisory-retired", "no-row"])
def test_gc_in_store_a_never_pairs_lane_x_with_the_checkout_store_b_made(
    two_stores, monkeypatch, capsys, a_row, managed, b_listed
):
    """Two checkouts of lane/x are registered in the one repository: store B's (made by B's `lane assign`) and one
    inside store A's folder (added with --force). Store A's gc may only ever take its own, whatever order Git lists
    them in, and must keep B's checkout and B's branch."""
    root_a, root_b, base, target = two_stores
    monkeypatch.chdir(root_a)
    if a_row == "advisory-retired":  # A's lane x: advisory, no checkout path in the row (made and retired by the CLI)
        assert cli.main([
            "--root", str(root_a), "lane", "assign", "--id", LANE, "--from", "lead", "--assignee", "dev",
            "--domain", "core", "--base", base, "--target", target, "--path", "src/core", "--advisory",
            "--no-worktree", "--worktree-waiver-reason", "coordination only",
        ]) == 0
        assert cli.main(["--root", str(root_a), "lane", "abandon", "--id", LANE]) == 0
    b_row = _assign(root_b, base, target)
    b_checkout = Path(b_row["worktree_path"])
    own = root_a / ".worktrees" / f"{LANE}-{base[:12]}-00000000"
    own.parent.mkdir(exist_ok=True)
    if managed:
        (own.parent / lanes.WORKTREE_MARKER_FILENAME).write_text("agenttalk managed worktrees\n", encoding="utf-8")
    added = subprocess.run(
        ["git", "-C", str(root_a), "worktree", "add", "--force", str(own), f"lane/{LANE}"],
        capture_output=True, text=True,
    )
    assert added.returncode == 0, added.stderr
    listed = _git(root_b, "worktree", "list", "--porcelain").replace("\\", "/")
    assert str(own).replace("\\", "/") in listed and str(b_checkout).replace("\\", "/") in listed  # both on lane/x

    real_records = cli._worktree_records

    def in_a(record) -> bool:
        return root_a in Path(str(record.get("worktree"))).resolve().parents

    def reordered(root):
        records = real_records(root)
        if records is None:
            return None
        return sorted(records, key=lambda rec: (not in_a(rec)) if b_listed == "last" else in_a(rec))

    monkeypatch.setattr(cli, "_worktree_records", reordered)
    on_x = [r for r in cli._worktree_records(root_a) if r.get("branch") == f"refs/heads/lane/{LANE}"]
    assert Path(on_x[-1 if b_listed == "last" else 0]["worktree"]).name == b_checkout.name  # the order asked for
    capsys.readouterr()

    rc = cli.main(["--root", str(root_a), "lane", "gc", "--delete", "--json"])

    out = capsys.readouterr().out
    print(f"[gc two checkouts a_row={a_row} managed={managed} b_{b_listed}] rc={rc} out={out.strip()!r}")
    _b_checkout_is_intact(root_b, b_checkout)
    assert _git(root_b, "branch", "--list", f"lane/{LANE}").strip()  # B's branch stays
    items = [row for row in json.loads(out)["items"] if row["lane_id"] == LANE]
    assert items and all(row.get("worktree") is None or root_a in Path(row["worktree"]).resolve().parents
                         for row in items)  # whatever gc paired, it is not B's folder
    assert all(row.get("branch_deleted") is not True for row in items)


# ---------------------------------------------------------------------------------------------------------------------
# Round 31 (zteam's read of round 30): a COPY of store B's checkout, its `.git` pointer file included, reaches the same
# administrative folder as the original and passes every check made from the checkout's side. Only Git's own record of
# where the checkout lives (the administrative folder's `gitdir` back-link) still names the original.
# ---------------------------------------------------------------------------------------------------------------------


def _copy_of_b_checkout_in_a(root_a: Path, b_row: dict, monkeypatch) -> Path:
    """B's checkout copied (`.git` file and all) into A's worktree folder, and A's row `x` (B's row) pointed at it:
    the copy is in place before the first check."""
    import shutil

    original = Path(b_row["worktree_path"])
    copy = root_a / ".worktrees" / ("copy-of-" + original.name)
    copy.parent.mkdir(exist_ok=True)
    shutil.copytree(original, copy, symlinks=True)
    row = json.loads(json.dumps(b_row))
    row["worktree_path"] = str(copy)
    row["worktree_toplevel_canonical"] = lanes.canonical_host_path(copy)
    monkeypatch.chdir(root_a)
    store_a = Store(root_a)
    data = lanes.load_lanes(store_a)
    data.setdefault("lanes", {})[LANE] = row
    lanes.save_lanes(store_a, data)
    return copy


@pytest.mark.parametrize("armed", [False, "_lane_finalize_delivery"], ids=["readable", "fails-at-final-recheck"])
def test_store_a_never_publishes_a_copy_of_store_b_checkout(two_stores, monkeypatch, capsys, armed):
    """zteam's P1: nothing is published, whether the copy stays readable or looking at it fails only from the final
    steps on. The refusal says what Git's own record says, and B's checkout, branch and lane are untouched."""
    from test_lanes import _checkout_cannot_be_resolved, _commit

    root_a, root_b, base, target = two_stores
    b_row = _assign(root_b, base, target)
    original = Path(b_row["worktree_path"])
    _commit(original, "src/core/a.py", "base\nB's work in progress\n")
    copy = _copy_of_b_checkout_in_a(root_a, b_row, monkeypatch)
    store_a = Store(root_a)
    if armed:  # looking at the copy fails only from the final steps on; otherwise it stays readable
        _checkout_cannot_be_resolved(monkeypatch, copy, only_during=armed)
    capsys.readouterr()

    rc = cli.main(["--root", str(root_a), "lane", "deliver", "--id", LANE, "--from", "dev"])

    err = capsys.readouterr().err
    assert rc == 3 and "Git's own record of this checkout says it lives at" in err, err
    deliveries = store_a.dir / lanes.DELIVERIES_DIRNAME
    left = [p.name for p in deliveries.rglob("*") if p.is_file() and not p.name.startswith(".")]
    assert not left, left
    row = lanes.load_lanes(store_a)["lanes"][LANE]
    assert row["status"] == lanes.STATUS_ACTIVE and "publish_pending" not in row and "delivery_artifact" not in row
    _b_checkout_is_intact(root_b, original)
    assert copy.is_dir()


def test_the_registration_of_a_copy_names_the_original_and_the_original_names_itself(two_stores):
    """The check itself: the administrative folder's record names the original checkout, so the original matches and
    its copy does not (a copy's `.git` file is the same, byte for byte)."""
    import shutil

    root_a, root_b, base, target = two_stores
    original = Path(_assign(root_b, base, target)["worktree_path"])
    copy = root_a / ".worktrees" / "copy"
    copy.parent.mkdir(exist_ok=True)
    shutil.copytree(original, copy, symlinks=True)
    admin = cli._git_dir(original)

    assert cli._lane_checkout_registration(str(original), admin) is None
    kind, named = cli._lane_checkout_registration(str(copy), admin)
    assert kind == "mismatch" and os.path.normcase(os.path.realpath(named)) == os.path.normcase(
        os.path.realpath(original / ".git")
    )
    assert cli._lane_checkout_registration(str(original), str(original / "no-such-admin-folder"))[0] == "unknown"


# ---------------------------------------------------------------------------------------------------------------------
# Round 31b (codex-rev's second item): a copy of B's checkout whose own `.git` entry is a LINK to B's `.git` file. Git's
# record names B's checkout, and resolving the copy's `.git` entry reaches that very file, so a check that resolved the
# entry would call the copy registered. The record's FOLDER is compared with this folder instead, and a `.git` entry
# that is itself a link is refused. (A file symlink cannot be made on every machine: most cases patch the resolution.)
# ---------------------------------------------------------------------------------------------------------------------


def _git_entry_resolves_to(monkeypatch, entry: Path, target: Path) -> None:
    """Looking at `entry` (strictly) answers as if it were a link to `target`."""
    real = os.path.realpath
    wanted = os.path.normcase(os.path.abspath(entry))

    def realpath(path, *args, strict=False, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if strict and os.path.normcase(os.path.abspath(os.fspath(path))) == wanted:
            return real(target, *args, strict=True, **kwargs)
        return real(path, *args, strict=strict, **kwargs)

    monkeypatch.setattr(os.path, "realpath", realpath)


def test_a_git_entry_that_leads_to_another_checkouts_git_file_is_not_this_checkouts_registration(
    two_stores, monkeypatch
):
    root_a, root_b, base, target = two_stores
    b_row = _assign(root_b, base, target)
    original = Path(b_row["worktree_path"])
    copy = _copy_of_b_checkout_in_a(root_a, b_row, monkeypatch)
    admin = cli._git_dir(str(original))
    _git_entry_resolves_to(monkeypatch, copy / ".git", original / ".git")

    assert cli._lane_checkout_registration(str(original), admin) is None
    kind, _named = cli._lane_checkout_registration(str(copy), admin)
    assert kind == "mismatch"


def test_store_a_never_publishes_a_copy_whose_git_entry_leads_to_b_git_file(two_stores, monkeypatch, capsys):
    from test_lanes import _commit

    root_a, root_b, base, target = two_stores
    b_row = _assign(root_b, base, target)
    original = Path(b_row["worktree_path"])
    _commit(original, "src/core/a.py", "base\nB's work in progress\n")
    copy = _copy_of_b_checkout_in_a(root_a, b_row, monkeypatch)
    store_a = Store(root_a)
    _git_entry_resolves_to(monkeypatch, copy / ".git", original / ".git")
    capsys.readouterr()

    rc = cli.main(["--root", str(root_a), "lane", "deliver", "--id", LANE, "--from", "dev"])

    err = capsys.readouterr().err
    assert rc == 3 and "Git's own record of this checkout says it lives at" in err, err
    left = [p.name for p in (store_a.dir / lanes.DELIVERIES_DIRNAME).rglob("*") if p.is_file() and p.name[0] != "."]
    assert not left, left
    assert lanes.load_lanes(store_a)["lanes"][LANE]["status"] == lanes.STATUS_ACTIVE
    _b_checkout_is_intact(root_b, original)


def test_a_git_entry_that_is_a_link_is_refused_whatever_it_leads_to(two_stores, monkeypatch, capsys):
    from agenttalk import lanes as lane_mod
    from test_lanes import _commit

    root_a, root_b, base, target = two_stores
    b_row = _assign(root_b, base, target)
    original = Path(b_row["worktree_path"])
    _commit(original, "src/core/a.py", "base\nB's work in progress\n")
    copy = _copy_of_b_checkout_in_a(root_a, b_row, monkeypatch)
    real_is_link = lane_mod._is_link
    wanted = os.path.normcase(os.path.abspath(copy / ".git"))
    monkeypatch.setattr(
        lane_mod, "_is_link",
        lambda path: True if os.path.normcase(os.path.abspath(path)) == wanted else real_is_link(path),
    )
    admin = cli._git_dir(str(original))

    assert cli._lane_checkout_registration(str(copy), admin) == ("link", str(copy / ".git"))
    assert cli._lane_checkout_registration(str(original), admin) is None
    capsys.readouterr()
    rc = cli.main(["--root", str(root_a), "lane", "deliver", "--id", LANE, "--from", "dev"])

    err = capsys.readouterr().err
    assert rc == 3 and "own `.git` entry" in err and "is a link" in err, err
    _b_checkout_is_intact(root_b, original)


def test_a_real_symbolic_link_for_the_git_entry_is_refused(two_stores, monkeypatch, capsys):
    """The arrangement itself, where the machine allows a file symlink (Windows without Developer Mode does not)."""
    from test_lanes import _commit

    root_a, root_b, base, target = two_stores
    b_row = _assign(root_b, base, target)
    original = Path(b_row["worktree_path"])
    _commit(original, "src/core/a.py", "base\nB's work in progress\n")
    copy = _copy_of_b_checkout_in_a(root_a, b_row, monkeypatch)
    (copy / ".git").unlink()
    try:
        os.symlink(original / ".git", copy / ".git")
    except OSError as exc:
        pytest.skip(f"this machine does not allow a file symlink here ({exc})")
    capsys.readouterr()

    rc = cli.main(["--root", str(root_a), "lane", "deliver", "--id", LANE, "--from", "dev"])

    assert rc != 0
    row = lanes.load_lanes(Store(root_a))["lanes"][LANE]
    assert row["status"] == lanes.STATUS_ACTIVE and "delivery_artifact" not in row
    _b_checkout_is_intact(root_b, original)
