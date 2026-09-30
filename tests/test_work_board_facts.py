"""wb-done-facts: lead-run verified-merges facts and the snapshot worker that reads them."""
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from agenttalk import cli, work_board
from agenttalk import work_board_facts as F
from agenttalk.envelope_snapshot import SnapshotService
from agenttalk.store import Store
from test_work_board_reducer import BUILDER, LEAD, POLICY, REVIEWER, Bus

MISSING = "c" * 40


def git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo),
                           *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """master holds a --no-ff merge of `merged`; `open` builds on it but is not merged."""
    path = tmp_path / "repo"
    git(tmp_path, "init", "-q", "-b", "master", str(path))
    git(path, "commit", "-q", "--allow-empty", "-m", "base")
    git(path, "switch", "-q", "-c", "merged")
    git(path, "commit", "-q", "--allow-empty", "-m", "merged work")
    merged = git(path, "rev-parse", "HEAD")
    git(path, "switch", "-q", "-c", "open")
    git(path, "commit", "-q", "--allow-empty", "-m", "open work")
    unmerged = git(path, "rev-parse", "HEAD")
    git(path, "switch", "-q", "master")
    git(path, "merge", "-q", "--no-ff", "-m", "merge", "merged")
    return path, merged, unmerged


def configure(store, **top):
    cfg = json.loads(store.config_path.read_text(encoding="utf-8"))
    cfg.update(top)
    store.config_path.write_text(json.dumps(cfg), encoding="utf-8")


def team(tmp_path, path, **entry):
    store = Store(tmp_path / "store")
    store.init([LEAD, BUILDER, REVIEWER])
    store.set_role(LEAD, "lead")
    configure(store, work_repos={"agenttalk": {"path": str(path), "targets": ["refs/heads/master"],
                                               "default": True, **entry}})
    return store


def publish(store, heads, **policy):
    """One built, independently reviewed item per slug, written as real envelopes dated now."""
    bus, now = Bus(), datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for slug, head in heads.items():
        bus.reply(bus.task(f"tk-build-{slug}", BUILDER, "build", item=slug, **POLICY, **policy), verdict="done")
        bus.reply(bus.task(f"tk-read-{slug}", REVIEWER, "read", item=slug, work_head=head), verdict="GO")
    for m in bus.messages:
        (store.messages_dir / f"{m.id}.json").write_text(json.dumps(replace(m, ts=now).to_dict()),
                                                          encoding="utf-8")


def board(store):
    service = SnapshotService(store)
    assert service.refresh()
    feed = service.board()
    return {i["work_item"]: i for i in feed["items"]}, feed


def test_merged_candidate_is_done_and_unmerged_is_not(tmp_path, repo, capsys):
    path, merged, unmerged = repo
    store = team(tmp_path, path)
    publish(store, {"item-merged": merged, "item-open": unmerged})
    assert cli.main(["--root", str(store.root), "board", "verify-merges"]) == 0
    assert "2 fact(s) (1 integrated, 1 not integrated), 0 unknown" in capsys.readouterr().out
    master = git(path, "rev-parse", "refs/heads/master")
    facts = json.loads((store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8"))["integration"]["facts"]
    assert {(f["work_item"], f["candidate"], f["result"]) for f in facts} == {
        ("item-merged", merged, "integrated"), ("item-open", unmerged, "not_integrated")}
    assert all(set(f) == F._FACT_KEYS and f["target_oid"] == master and f["repo_alias"] == "agenttalk"
               and f["target_ref"] == "refs/heads/master" and f["project"] == store.project_id() for f in facts)
    items, feed = board(store)
    assert items["item-merged"]["workflow_column"] == "done"
    assert items["item-merged"]["integration"][merged]["target_oid"] == master
    assert (items["item-open"]["workflow_column"], items["item-open"]["integration"]) == ("ready", {unmerged: False})
    assert not [e for e in feed["errors"] if "integration" in e]


def test_dry_run_writes_nothing_and_unconfigured_exits_2(tmp_path, repo, capsys):
    path, merged, _ = repo
    store = team(tmp_path, path)
    publish(store, {"item-merged": merged})
    assert cli.main(["--root", str(store.root), "board", "verify-merges", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["facts"][0]["result"] == "integrated"
    assert not (store.state_dir / F.FACTS_FILE).exists()
    configure(store, work_repos={})
    assert cli.main(["--root", str(store.root), "board", "verify-merges"]) == 2
    assert "no usable work_repos alias" in capsys.readouterr().err


def test_stale_fact_keeps_the_derived_column_and_says_so(tmp_path, repo):
    path, merged, _ = repo
    store = team(tmp_path, path)
    publish(store, {"item-merged": merged})
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    F.verify_merges(store, now=old)
    items, _ = board(store)
    card = items["item-merged"]
    assert (card["workflow_column"], card["integration"]) == ("ready", {})
    assert card["reason"].endswith(f"; integration evidence stale (as of {old.isoformat()})")
    configure(store, integration_facts_max_age_seconds=26 * 3600)  # a configured window
    assert board(store)[0]["item-merged"]["workflow_column"] == "done"
    F.verify_merges(store, now=datetime.now(timezone.utc) + timedelta(hours=1))  # from the future
    assert board(store)[0]["item-merged"]["workflow_column"] == "ready"
    assert [F.max_age_seconds({"integration_facts_max_age_seconds": v}) for v in (True, 59, 120)] == [
        86400, 86400, 120]


@pytest.mark.parametrize("change", [{"targets": ["refs/heads/release"]}, "path", "project"])
def test_remapped_alias_ignores_old_proof(tmp_path, repo, change):
    path, merged, _ = repo
    store = team(tmp_path, path)
    publish(store, {"item-merged": merged})
    F.verify_merges(store)
    if change == "project":  # proof recorded for another project
        facts = store.state_dir / F.FACTS_FILE
        facts.write_text(facts.read_text(encoding="utf-8").replace(store.project_id(), "other-project"),
                         encoding="utf-8")
    else:
        entry = {"path": str(path), "targets": ["refs/heads/master"], "default": True}
        entry.update({"path": str(tmp_path)} if change == "path" else change)
        configure(store, work_repos={"agenttalk": entry})
    items, feed = board(store)
    assert items["item-merged"]["workflow_column"] == "ready"
    assert "1 integration fact(s) ignored: repository mapping changed" in feed["errors"]


@pytest.mark.parametrize("repos,problem", [
    ({"agenttalk": {"targets": ["--output=x"]}}, "targets must be"),
    ({"agenttalk": {"targets": ["master"]}}, "targets must be"),  # a short name can be shadowed by a tag
    ({"agenttalk": {"targets": ["refs/heads/a..b"]}}, "targets must be"),
    ({"agenttalk": {"targets": ["refs/heads/a/.hidden"]}}, "targets must be"),
    ({"agenttalk": {"targets": ["refs/tags/v1"]}}, "targets must be"),
    ({"agenttalk": {"path": "relative/repo"}}, "path must be absolute"),
    ({"agenttalk": {"path": "ESCAPE"}}, "path must be absolute"),
    ({"--git-dir": {}}, "not a lowercase slug"),
    ({"agenttalk": {}, "second": {}}, "bind the same checkout"),
])
def test_hostile_alias_config_is_rejected(tmp_path, repos, problem):
    base = {"path": str(tmp_path), "targets": ["refs/heads/master"]}
    raw = {alias: {**base, **(
        {"path": str(tmp_path / ".." / tmp_path.name)} if entry.get("path") == "ESCAPE" else entry)}
        for alias, entry in repos.items()}
    aliases, default, problems = F.repo_aliases({"work_repos": raw})
    assert (aliases, default) == ({}, None) and problem in problems[0]


def test_two_defaults_leave_no_default(tmp_path):
    raw = {name: {"path": str(tmp_path / name), "targets": ["refs/heads/master"], "default": True}
           for name in ("one", "two")}
    aliases, default, problems = F.repo_aliases({"work_repos": raw})
    assert set(aliases) == {"one", "two"} and default is None and "more than one" in problems[0]


def test_checkout_must_be_the_top_of_its_work_tree(tmp_path, repo):
    path, merged, _ = repo
    (path / "sub").mkdir()
    store = team(tmp_path, path / "sub")
    publish(store, {"item-merged": merged})
    result = F.verify_merges(store)
    assert result["facts"] == [] and result["unknown"] == [
        ("item-merged", "checkout path is not the top of a Git work tree")]


def test_git_probe_is_hardened_and_allowlisted(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(F.subprocess, "run", lambda argv, **kw: seen.update(argv=argv, **kw))
    F._git(tmp_path, "merge-base", "--is-ancestor", "a" * 40, "b" * 40)
    assert seen["argv"][1:6] == ["--no-replace-objects", "-c", "core.fsmonitor=false", "-C", str(tmp_path)]
    assert seen["timeout"] == 2.0 and "shell" not in seen and "GIT_DIR" not in seen["env"]
    assert {k: seen["env"][k] for k in ("GIT_NO_LAZY_FETCH", "GIT_NO_REPLACE_OBJECTS", "GIT_TERMINAL_PROMPT")} == {
        "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0"}
    for probe in (("status",), ("rev-parse", "--output=x"), ("rev-parse", "HEAD"), ("merge-base", "a" * 40),
                  ("cat-file", "-e", "--textconv"), ("rev-parse", "refs/heads/-x")):
        with pytest.raises(ValueError):
            F._git(tmp_path, *probe)


def test_fact_cap_and_ambiguous_binding_record_nothing_extra(tmp_path, repo, monkeypatch):
    path, merged, unmerged = repo
    store = team(tmp_path, path)
    publish(store, {"item-merged": merged, "item-open": unmerged})
    monkeypatch.setattr(F, "MAX_FACTS", 1)
    result = F.verify_merges(store, dry_run=True)
    assert len(result["facts"]) == 1 and ("item-open", "fact limit reached") in result["unknown"]
    monkeypatch.setattr(F, "_candidates", lambda store, cfg: [
        {"work_item": "item-merged", "candidate": merged, "repo_binding": "ambiguous"}])
    assert F.verify_merges(store, dry_run=True)["unknown"] == [
        ("item-merged", "conflicting repository declarations")]


def test_symlinked_or_junction_checkout_is_refused(tmp_path, repo):
    path, merged, _ = repo
    link = tmp_path / "link"
    try:
        os.symlink(path, link, target_is_directory=True)
    except OSError:
        try:
            import _winapi
            _winapi.CreateJunction(str(path), str(link))
        except (ImportError, OSError):
            pytest.skip("neither symlinks nor junctions can be created here")
    store = team(tmp_path, link)
    publish(store, {"item-merged": merged})
    result = F.verify_merges(store)
    assert result["facts"] == [] and "not canonical" in result["unknown"][0][1]


@pytest.mark.parametrize("decoy", ["tag-shadow", "nested-branch"])
def test_ambiguous_target_ref_is_never_proof(tmp_path, repo, decoy):
    path, merged, _ = repo
    master = git(path, "rev-parse", "HEAD")
    if decoy == "tag-shadow":  # a tag named like the branch makes the full ref ambiguous
        git(path, "update-ref", "refs/tags/refs/heads/master", master)
        target = "refs/heads/master"
    else:  # no refs/heads/release, but a branch literally named refs/heads/release resolves
        git(path, "update-ref", "refs/heads/refs/heads/release", master)
        target = "refs/heads/release"
    store = team(tmp_path, path, targets=[target])
    publish(store, {"item-merged": merged})
    result = F.verify_merges(store)
    assert result["facts"] == [] and "missing or ambiguous" in result["unknown"][0][1]


def test_shallow_clone_and_missing_object_record_no_fact(tmp_path, repo):
    path, merged, _ = repo
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", "1", path.as_uri(), str(shallow))
    store = team(tmp_path, shallow)
    publish(store, {"item-merged": merged})
    result = F.verify_merges(store)
    assert result["facts"] == [] and result["unknown"] == [("item-merged", "shallow or incomplete history")]
    other = team(tmp_path / "second", path)
    publish(other, {"item-lost": MISSING})
    result = F.verify_merges(other)
    assert result["facts"] == [] and result["unknown"] == [
        ("item-lost", "candidate commit is not available locally")]


def test_probe_timeout_stops_the_checkout_and_records_nothing(tmp_path, repo, monkeypatch):
    path, merged, unmerged = repo
    store = team(tmp_path, path)
    publish(store, {"item-merged": merged, "item-open": unmerged})
    calls, stub = tmp_path / "hung-probes.txt", tmp_path / "git_stub.py"
    stub.write_text(  # real git, except that every cat-file hangs
        "import subprocess, sys, time\n"
        "if 'cat-file' in sys.argv:\n"
        f"    open({str(calls)!r}, 'a').write('x')\n"
        "    time.sleep(60)\n"
        "r = subprocess.run(['git', *sys.argv[1:]], capture_output=True)\n"
        "sys.stdout.buffer.write(r.stdout)\nsys.stderr.buffer.write(r.stderr)\nsys.exit(r.returncode)\n",
        encoding="utf-8")
    monkeypatch.setattr(F, "GIT", (sys.executable, str(stub)))
    monkeypatch.setattr(F, "PROBE_TIMEOUT_SECONDS", 3.0)
    result = F.verify_merges(store)
    assert result["facts"] == [] and {r for _, r in result["unknown"]} == {"git unavailable or timed out"}
    assert calls.read_text(encoding="utf-8") == "x"  # one hung probe, then the checkout is abandoned


@pytest.mark.parametrize("content,warning", [
    (None, "integration facts missing"),
    (b"{not json", "integration facts malformed"),
    (b" " * (F.MAX_FILE_BYTES + 1), "exceed their size bound"),
    (json.dumps({"schema_version": 2}).encode(), "schema unsupported"),
    (json.dumps({"schema_version": 1, "integration": {"facts": [{"work_item": "x"}]}}).encode(),
     "integration facts malformed"),
], ids=["missing", "malformed", "oversize", "schema", "bad-fact"])
def test_unusable_facts_file_means_no_facts_and_a_warning(tmp_path, repo, content, warning):
    path, merged, _ = repo
    store = team(tmp_path, path)
    publish(store, {"item-merged": merged})
    F.verify_merges(store)
    target = store.state_dir / F.FACTS_FILE
    if content is None:
        target.unlink()
    else:
        target.write_bytes(content)
    items, feed = board(store)
    assert items["item-merged"]["workflow_column"] == "ready"
    assert any(warning in e for e in feed["errors"])


def test_unconfigured_project_without_facts_has_no_warning(tmp_path, repo):
    path, merged, _ = repo
    store = team(tmp_path, path)
    cfg = json.loads(store.config_path.read_text(encoding="utf-8"))
    del cfg["work_repos"]
    store.config_path.write_text(json.dumps(cfg), encoding="utf-8")
    publish(store, {"item-merged": merged})
    items, feed = board(store)
    assert items["item-merged"]["workflow_column"] == "ready"
    assert not [e for e in feed["errors"] if "integration" in e]


def test_snapshot_worker_reads_facts_without_running_git(tmp_path, repo, monkeypatch):
    path, merged, _ = repo
    store = team(tmp_path, path)
    publish(store, {"item-merged": merged})
    F.verify_merges(store)

    def refuse(*args, **kwargs):
        raise AssertionError("the snapshot worker must not start a process")
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, refuse)
    monkeypatch.setattr(os, "system", refuse)
    assert board(store)[0]["item-merged"]["workflow_column"] == "done"


def test_declared_repository_and_target_select_evidence(tmp_path, repo):
    path, merged, _ = repo
    cases = [({"work_repo": "elsewhere"}, "not an approved work_repos alias"),
             ({"work_repo": "agenttalk", "work_target": "release"}, "is not approved for agenttalk"),
             ({"work_repo": "agenttalk", "work_target": "master"}, None)]
    for n, (policy, reason) in enumerate(cases):
        store = team(tmp_path / str(n), path)
        publish(store, {"item-merged": merged}, **policy)
        result = F.verify_merges(store)
        if reason:
            assert result["facts"] == [] and reason in result["unknown"][0][1]
        else:
            assert [f["result"] for f in result["facts"]] == ["integrated"]


def test_reducer_repo_binding_is_null_agreed_or_ambiguous():
    def binding(*policies):
        bus = Bus()
        for n, policy in enumerate(policies):
            bus.task(f"tk-{n}", BUILDER, "build", **POLICY, **policy)
        return work_board.reduce(bus.messages, lead=LEAD)["items"][0]["repo_binding"]
    assert binding({}) is None
    assert binding({"work_repo": "agenttalk", "work_target": "master"}) == {"repo": "agenttalk",
                                                                           "target": "master"}
    assert binding({"work_repo": "agenttalk"}, {"work_repo": "other"}) == "ambiguous"


def test_verify_merges_keeps_other_sections(tmp_path, repo):
    path, merged, _ = repo
    store = team(tmp_path, path)
    publish(store, {"item-merged": merged})
    (store.state_dir / F.FACTS_FILE).write_text(json.dumps({"schema_version": 1, "plans": {"p": 1}}),
                                                 encoding="utf-8")
    F.verify_merges(store)
    doc = json.loads((store.state_dir / F.FACTS_FILE).read_text(encoding="utf-8"))
    assert doc["plans"] == {"p": 1} and len(doc["integration"]["facts"]) == 1



# ---------------------------------------------------------------- fix round 1 (#247 cold read)

def publish_cycle(store, slug, head, cycle, **policy):
    """A later build/read cycle for the same item, continuing the store's message ids."""
    bus, now = Bus(), datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    bus.messages = list(store.valid_messages())
    first = len(bus.messages)
    bus.reply(bus.task(f"tk-build-{slug}-c{cycle}", BUILDER, "build", item=slug, work_cycle=cycle,
                       **POLICY, **policy), verdict="done")
    bus.reply(bus.task(f"tk-read-{slug}-c{cycle}", REVIEWER, "read", item=slug, work_cycle=cycle,
                       work_head=head), verdict="GO")
    for m in bus.messages[first:]:
        (store.messages_dir / f"{m.id}.json").write_text(json.dumps(replace(m, ts=now).to_dict()),
                                                          encoding="utf-8")


def test_default_repo_switch_needs_proof_in_the_new_repo(tmp_path, repo):
    path, merged, _ = repo
    store = team(tmp_path, path)
    publish(store, {"item-merged": merged})
    F.verify_merges(store)
    assert board(store)[0]["item-merged"]["workflow_column"] == "done"
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(path), str(other))
    configure(store, work_repos={
        "agenttalk": {"path": str(path), "targets": ["refs/heads/master"], "default": False},
        "other": {"path": str(other), "targets": ["refs/heads/master"], "default": True}})
    card = board(store)[0]["item-merged"]
    assert (card["workflow_column"], card["integration"]) == ("ready", {})
    assert card["reason"].endswith("; integration evidence ignored: recorded for another repository or target")
    F.verify_merges(store)  # proof in the current default repo is accepted again
    card = board(store)[0]["item-merged"]
    assert card["workflow_column"] == "done" and card["integration"][merged]["repo_alias"] == "other"


def test_new_cycle_target_needs_proof_for_that_target(tmp_path, repo):
    path, merged, _ = repo
    git(path, "update-ref", "refs/heads/release", git(path, "rev-parse", merged + "^"))
    store = team(tmp_path, path, targets=["refs/heads/master", "refs/heads/release"])
    publish(store, {"item-merged": merged}, work_repo="agenttalk", work_target="master")
    F.verify_merges(store)
    assert board(store)[0]["item-merged"]["workflow_column"] == "done"
    publish_cycle(store, "item-merged", merged, 2, work_repo="agenttalk", work_target="release")
    card = board(store)[0]["item-merged"]
    assert card["repo_binding"] == {"repo": "agenttalk", "target": "release"}
    assert card["workflow_column"] == "ready" and "integration evidence ignored" in card["reason"]
    result = F.verify_merges(store)  # the release proof is negative, and it is the one that counts
    assert [(f["target_ref"], f["result"]) for f in result["facts"]] == [("refs/heads/release", "not_integrated")]
    card = board(store)[0]["item-merged"]
    assert (card["workflow_column"], card["integration"]) == ("ready", {merged: False})


def test_overflowing_timestamp_is_rejected_and_the_board_projects(tmp_path, repo):
    path, merged, _ = repo
    store = team(tmp_path, path)
    publish(store, {"item-merged": merged})
    F.verify_merges(store)
    target = store.state_dir / F.FACTS_FILE
    doc = json.loads(target.read_text(encoding="utf-8"))
    doc["integration"]["facts"][0]["checked_at"] = "9999-12-31T23:59:59-23:00"  # parses, then overflows UTC
    target.write_text(json.dumps(doc), encoding="utf-8")
    items, feed = board(store)
    assert feed["coverage"]["status"] == "complete" and items["item-merged"]["workflow_column"] == "ready"
    assert "integration facts malformed" in feed["errors"]


@pytest.mark.parametrize("stage", ["_load_integration", "integration_for"])
def test_any_fault_in_optional_evidence_leaves_the_board_projecting(tmp_path, repo, monkeypatch, stage):
    path, merged, _ = repo
    store = team(tmp_path, path)
    publish(store, {"item-merged": merged})
    F.verify_merges(store)

    def broken(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(F, stage, broken)
    items, feed = board(store)
    assert feed["coverage"]["status"] == "complete" and items["item-merged"]["workflow_column"] == "ready"
    word = "facts" if stage == "_load_integration" else "evidence"
    assert f"integration {word} unusable (RuntimeError)" in feed["errors"]


def test_integration_for_selects_only_the_current_binding(tmp_path):
    now = datetime.now(timezone.utc)
    cfg = {"work_repos": {"agenttalk": {"path": str(tmp_path), "default": True,
                                        "targets": ["refs/heads/master", "refs/heads/release"]}}}

    def fact(target, result, age=0):
        return {"work_item": "item", "candidate": MISSING, "repo_alias": "agenttalk", "target_ref": target,
                "target_oid": "d" * 40, "checked_at": "x", "result": result,
                "checked": now - timedelta(hours=age), "fresh": age < 24}

    def pick(binding, *facts):
        item = {"work_item": "item", "candidate": MISSING, "repo_binding": binding}
        integrated, notes = F.integration_for([item], {"facts": list(facts), "config": cfg})
        return integrated.get(("item", MISSING)), notes.get(("item", MISSING))
    both = (fact("refs/heads/master", "integrated"), fact("refs/heads/release", "not_integrated"))
    assert pick(None, *both)[0]["target_ref"] == "refs/heads/master"  # merged into one approved target
    assert pick({"repo": None, "target": "release"}, *both) == (False, None)
    assert pick({"repo": None, "target": "master"}, fact("refs/heads/master", "integrated", age=30))[1][0] \
        .startswith("integration evidence stale (as of ")
    assert pick("ambiguous", *both) == (None, (
        "integration evidence ignored: conflicting repository declarations", None))
    assert pick({"repo": "elsewhere", "target": None}, *both)[1][0] == (
        "integration evidence ignored: repository 'elsewhere' is not an approved work_repos alias")
