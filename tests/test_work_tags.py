"""B2a publication contracts; synthetic local bus, no worker processes."""

import pytest

from agenttalk import cli, reply_transport
from agenttalk.install_skills import SKILLS_ROOT
from agenttalk.store import Store


@pytest.fixture
def bus(tmp_path):
    store = Store(tmp_path)
    store.init(["lead", "worker", "reviewer"])
    store.set_role("lead", "lead")
    return store


def task(bus, **extra):
    meta = {"work_item": "widget", "stage": "build", "request_id": "tk-original", **extra}
    return bus.send(sender="lead", recipient="worker", kind="task", body="work", meta=meta)


def command(bus, *args):
    try:
        return cli.main(["--root", str(bus.root), *args])
    except SystemExit as exc:
        return exc.code


@pytest.mark.parametrize("meta", ["work_item=Bad", "work_item=a/b", "work_item=" + "a" * 65,
    "stage=ship", "work_cycle=0", "work_cycle=-1", "work_round=1.5", "work_cycle=+2",
    "work_head=abc", "work_title=" + "x" * 161])
def test_invalid_metadata_refused_before_send(bus, meta, capsys):
    assert command(bus, "task", "--from", "lead", "--to", "worker", "--force", "-m", "work",
                   "--meta", meta) == 2
    assert not bus.valid_messages()
    # #297: a refusal here must never print anything that reads as proof of a send -
    # in particular no auto-minted request id, for every shape of invalid metadata
    # this table covers, not only the two (work_item, stage) issue #297 names.
    out, err = capsys.readouterr()
    assert out == "" and "tk-" not in err


def test_work_item_refusal_names_the_value_and_a_corrected_example(bus, capsys):
    """#297: the refusal must name the refused value and suggest an accepted one, not
    just repeat the rule - a dotted release tag is the exact case that cost real time."""
    assert command(bus, "task", "--from", "lead", "--to", "worker", "--force", "-m", "work",
                   "--meta", "work_item=release-0.96.0") == 2
    err = capsys.readouterr().err
    assert 'work_item "release-0.96.0" is not allowed' in err
    assert '"release-0-96-0"' in err


def test_stage_refusal_names_the_value_and_every_accepted_one(bus, capsys):
    assert command(bus, "task", "--from", "lead", "--to", "worker", "--force", "-m", "work",
                   "--meta", "stage=review") == 2
    err = capsys.readouterr().err
    assert 'stage "review" is not allowed' in err
    assert "design, build, read, fix, delta, sweep" in err


def test_flags_and_equivalent_metadata(bus):
    assert command(bus, "task", "--from", "lead", "--to", "worker", "--force", "-m", "work",
                   "--work-item", "widget", "--stage", "build", "--work-cycle", "02",
                   "--work-round", "1", "--work-head", "a" * 40, "--meta", "work_cycle=2") == 0
    meta = bus.valid_messages()[0].meta
    assert {key: meta[key] for key in ("work_item", "stage", "work_cycle", "work_round", "work_head")} == {
        "work_item": "widget", "stage": "build", "work_cycle": "2", "work_round": "1", "work_head": "a" * 40}


@pytest.mark.parametrize("flags", [["--work-item", "widget", "--meta", "work_item=other"],
    ["--meta", "work_item=widget", "--meta", "work_item=other"]])
def test_conflicting_metadata_refused(bus, flags):
    assert command(bus, "task", "--from", "lead", "--to", "worker", "--force", "-m", "work", *flags) == 2
    assert not bus.valid_messages()


@pytest.mark.parametrize("stage,word,expected", [("build", "DoNe", "done"), ("design", "DONE", "done"),
    ("fix", "done", "done"), ("read", "go", "GO"), ("delta", "Fix", "FIX"), ("sweep", "hold", "HOLD")])
def test_reply_inherits_and_normalizes(bus, stage, word, expected):
    opener = task(bus, stage=stage, work_cycle="2", work_head="a" * 40)
    assert command(bus, "reply", "--from", "worker", "--to-id", opener.id, "--kind", "task-response",
                   "--meta", "status=done", "--meta", "verdict=" + word, "-m", "result") == 0
    meta = bus.messages_for("lead")[-1].meta
    assert (meta["work_item"], meta["work_cycle"], meta["stage"], meta["work_head"], meta["verdict"]) == (
        "widget", "2", stage, "a" * 40, expected)


@pytest.mark.parametrize("verdict,issue", [(None, "verdict missing"), ("READY", "unrecognized verdict"),
    ("not ready", "unrecognized verdict"), ("approved", "unrecognized verdict")])
def test_no_verdict_guessed_from_body(bus, verdict, issue):
    task(bus, stage="read")
    meta = {"request_id": "tk-original", "status": "done"}
    if verdict is not None:
        meta["verdict"] = verdict
    reply = bus.send(sender="worker", recipient="lead", kind="task-response", body="GO", meta=meta)
    assert reply.meta["verdict_issue"] == issue
    if verdict is None:
        assert "verdict" not in reply.meta


def test_draft_and_cli_missing_verdict_parity(bus, tmp_path):
    opener = task(bus, stage="read")
    draft = tmp_path / "reply.md"
    draft.write_text("GO", encoding="utf-8")
    result = reply_transport.deliver_draft_reply(bus, agent="worker", record=opener.to_dict(), draft_path=draft)
    assert result is not None
    assert command(bus, "reply", "--from", "worker", "--to-id", opener.id, "--kind", "task-response",
                   "--meta", "status=done", "-m", "GO") == 0
    actual = bus.messages_for("lead")[-1]
    keys = ("work_item", "stage", "status", "verdict_issue")
    assert {k: result.meta[k] for k in keys} == {k: actual.meta[k] for k in keys}
    assert result.meta["verdict_issue"] == "verdict missing"
    assert "verdict" not in result.meta and "verdict" not in actual.meta


@pytest.mark.parametrize("field,value", [("work_item", "other"), ("work_cycle", "2"), ("stage", "read"),
                                       ("work_head", "b" * 40)])
def test_reply_cannot_contradict_opener(bus, field, value):
    task(bus, stage="read", work_head="a" * 40)
    if field == "stage":
        value = "build"
    with pytest.raises(ValueError):
        bus.send(sender="worker", recipient="lead", kind="task-response", body="GO",
                 meta={"request_id": "tk-original", field: value})


@pytest.mark.parametrize("status,verdict,valid", [("approved", "GO", True), ("rejected", "FIX", True),
    ("rejected", "HOLD", True), ("needs-info", "HOLD", True), ("approved", "FIX", False),
    ("needs-info", "GO", False)])
def test_native_review_status_consistency(bus, status, verdict, valid):
    bus.send(sender="lead", recipient="reviewer", kind="review-request", body="review",
             meta={"request_id": "rq-original", "work_item": "widget", "stage": "read"})
    def reply():
        return bus.send(sender="reviewer", recipient="lead", kind="review-result", body="result",
                        meta={"request_id": "rq-original", "status": status, "verdict": verdict})
    if valid:
        assert reply().meta["verdict"] == verdict
    else:
        with pytest.raises(ValueError):
            reply()


@pytest.mark.parametrize("stage", ["build", "read"])
def test_declined_replacement_preserves_history(bus, stage):
    task(bus, stage=stage)
    declined = bus.send(sender="worker", recipient="lead", kind="task-response", body="cannot",
                        meta={"request_id": "tk-original", "status": "declined"})
    assert command(bus, "task", "--from", "lead", "--to", "reviewer", "--force", "-m", "replace",
                   "--work-item", "widget", "--stage", stage, "--supersedes", "tk-original") == 0
    replacement = bus.messages_for("reviewer")[-1]
    assert replacement.meta["supersedes"] == "tk-original"
    assert declined in bus.valid_messages()
    assert declined.meta["status"] == "declined" and "verdict" not in declined.meta


@pytest.mark.parametrize("fault", ["item", "cycle", "missing", "self", "branch", "authority", "cycle-link"])
def test_invalid_supersession_refused(bus, fault):
    task(bus)
    meta = {"request_id": "tk-next", "supersedes": "tk-original", "work_item": "widget", "stage": "build"}
    sender = "lead"
    if fault == "item":
        meta["work_item"] = "other"
    elif fault == "cycle":
        meta["work_cycle"] = "2"
    elif fault == "missing":
        meta["supersedes"] = "another-root-request"
    elif fault == "self":
        meta["supersedes"] = "tk-next"
    elif fault == "authority":
        sender = "reviewer"
    else:
        bus.send(sender=sender, recipient="worker", kind="task", body="replace", meta=meta)
        meta = dict(meta, request_id="tk-third")
        if fault == "cycle-link":
            meta.update(request_id="tk-original", supersedes="tk-next")
    with pytest.raises(ValueError):
        bus.send(sender=sender, recipient="worker", kind="task", body="replace", meta=meta)


def test_lead_skill_twins_work_contract():
    bodies = [(SKILLS_ROOT / name).read_text(encoding="utf-8") for name in
              ("claude/agenttalk.lead.md", "codex/agenttalk-lead/SKILL.md")]
    sections = [body.split("## Work-item protocol", 1)[1].split("\n## ", 1)[0] for body in bodies]
    assert sections[0] == sections[1]
    for text in ("supersedes", "verdict missing", "READY/NOT READY", "different-seat", "no_gates_reason",
                 "assignee_model_vendors", "requester-only", "B6a isolation", "later B6b", "provenance is Unknown"):
        assert text in sections[0]


@pytest.mark.parametrize("stage", ["build", "read"])
def test_missing_status_is_not_invented(bus, stage):
    task(bus, stage=stage)
    reply = bus.send(sender="worker", recipient="lead", kind="task-response", body="done",
                     meta={"request_id": "tk-original", "verdict": "GO" if stage == "read" else "done"})
    assert "status" not in reply.meta


def test_builder_can_report_output_head(bus):
    task(bus, work_head="a" * 40)
    reply = bus.send(sender="worker", recipient="lead", kind="task-response", body="built",
                     meta={"request_id": "tk-original", "work_head": "B" * 64, "verdict": "done"})
    assert reply.meta["work_head"] == "b" * 64
    assert "work_cycle" not in reply.meta  # Legacy cycle remains visibly absent.


@pytest.mark.parametrize("fault", ["participant", "kind", "anchor", "missing-anchor", "ambiguous"])
def test_reply_requires_unique_correlated_opener(bus, fault):
    task(bus)
    meta = {"request_id": "tk-original"}
    if fault == "anchor":
        other = task(bus, request_id="tk-other")
        meta["in_reply_to"] = other.id
    if fault == "missing-anchor":
        meta["in_reply_to"] = "unavailable-message"
    if fault == "ambiguous":
        task(bus, work_item="other")
    with pytest.raises(ValueError):
        bus.send(sender="reviewer" if fault == "participant" else "worker", recipient="lead",
                 kind="review-result" if fault == "kind" else "task-response", body="GO", meta=meta)


def test_native_review_replacement_by_current_lead(bus):
    bus.send(sender="worker", recipient="reviewer", kind="review-request", body="read",
             meta={"request_id": "rq-old", "work_item": "widget", "stage": "read", "work_cycle": 2})
    new = bus.send(sender="lead", recipient="reviewer", kind="review-request", body="read again",
                   meta={"request_id": "rq-new", "work_item": "widget", "stage": "delta",
                         "work_cycle": "02", "supersedes": "rq-old"})
    assert new.meta["work_cycle"] == "2"
    assert len(bus.valid_messages()) == 2  # Outstanding old review is not cancelled.


def test_competing_replacement_rechecked_at_publication(bus, monkeypatch):
    from agenttalk import work_tags
    task(bus)
    normalize = work_tags.normalize
    def race(store, sender, recipient, kind, meta):
        result = normalize(store, sender, recipient, kind, meta)
        if meta.get("request_id") == "tk-loser":
            task(bus, request_id="tk-winner", supersedes="tk-original")
        return result
    monkeypatch.setattr(work_tags, "normalize", race)
    with pytest.raises(ValueError, match="branching replacement"):
        task(bus, request_id="tk-loser", supersedes="tk-original")
    assert {m.meta["request_id"] for m in bus.valid_messages()} == {"tk-original", "tk-winner"}


def test_rescind_supersedes_keeps_existing_generation_namespace(bus):
    scoped_generation = {"inbound_id": "generation"}
    message = bus.send(sender="lead", recipient="worker", kind="rescind", body="cancel",
                       meta={"request_id": "old-question", "supersedes": scoped_generation})
    assert message.meta["supersedes"] == scoped_generation


def test_replacement_inherits_dispatch_policy(bus):
    task(bus, work_repo="repo", work_branch="feature", work_target="master",
         required_gates="[]", no_gates_reason="external CI")
    replacement = task(bus, request_id="tk-next", supersedes="tk-original")
    for key in ("work_repo", "work_branch", "work_target", "required_gates", "no_gates_reason"):
        assert replacement.meta[key] == bus.valid_messages()[0].meta[key]
    with pytest.raises(ValueError, match="dispatch policy"):
        task(bus, request_id="tk-third", supersedes="tk-next", work_repo="other")


def test_declined_replacement_can_itself_be_replaced(bus):
    task(bus)
    task(bus, request_id="tk-next", supersedes="tk-original")
    declined = bus.send(sender="worker", recipient="lead", kind="task-response", body="done? no",
                        meta={"request_id": "tk-next", "status": "declined"})
    task(bus, request_id="tk-final", supersedes="tk-next")
    assert declined.meta["status"] == "declined" and declined.meta["verdict_issue"] == "verdict missing"
    assert len(bus.valid_messages()) == 4


def external():
    return {"external_deliverable": "true", "stage": "read", "work_head": "a" * 40,
            "work_repo": "repo", "work_branch": "feature", "work_target": "master",
            "required_gates": "[]", "no_gates_reason": "external CI"}


@pytest.mark.parametrize("field,bad", [("external_deliverable", "yes"), ("external_deliverable", 1),
    ("work_head", None), ("work_repo", None), ("work_branch", None), ("work_target", None),
    ("required_gates", None), ("required_gates", "{}"), ("required_gates", '["bad/key"]'),
    ("no_gates_reason", None), ("stage", "build"), ("external_deliverable", "TRUE"),
    ("work_repo", ""), ("required_gates", '["unit"]'), ("no_gates_reason", "x" * 1025)])
def test_external_declaration_refuses_incomplete_or_invalid(bus, field, bad):
    meta = external()
    meta[field] = bad
    if bad is None:
        del meta[field]
    with pytest.raises(ValueError):
        task(bus, **meta)
    assert not bus.valid_messages()


@pytest.mark.parametrize("raw,expected", [(True, True), ("true", True), (False, False), ("false", False)])
def test_external_normalization_and_inheritance(bus, tmp_path, raw, expected):
    opener = task(bus, **dict(external(), external_deliverable=raw))
    replacement = task(bus, request_id="tk-next", supersedes="tk-original", stage="delta", work_head="a" * 40)
    assert opener.meta["external_deliverable"] is expected
    assert replacement.meta["external_deliverable"] is expected
    draft = tmp_path / "reply.md"
    draft.write_text("GO", encoding="utf-8")
    reply = reply_transport.deliver_draft_reply(bus, agent="worker", record=opener.to_dict(), draft_path=draft)
    assert reply.meta["external_deliverable"] is expected
    with pytest.raises(ValueError):
        bus.send(sender="worker", recipient="lead", kind="task-response", body="GO",
                 meta={"request_id": "tk-original", "external_deliverable": not expected})
    with pytest.raises(ValueError):
        task(bus, request_id="tk-third", supersedes="tk-next", **dict(external(), external_deliverable=not expected))


def test_external_requires_lead_and_allows_explicit_checks(bus):
    meta = dict(external(), request_id="rq-ext", work_item="widget", required_gates='["unit"]')
    del meta["no_gates_reason"]
    with pytest.raises(ValueError):
        bus.send(sender="worker", recipient="reviewer", kind="review-request", body="review", meta=meta)
    assert bus.send(sender="lead", recipient="reviewer", kind="review-request", body="review", meta=meta)


@pytest.mark.parametrize("raw,expected", [("true", True), ("false", False)])
def test_external_cli_metadata(bus, raw, expected):
    flags = [arg for k, v in dict(external(), external_deliverable=raw).items() for arg in ("--meta", k + "=" + v)]
    assert command(bus, "task", "--from", "lead", "--to", "worker", "--force", "-m", "review", *flags) == (
        2 if expected else 0)
    assert command(bus, "task", "--from", "lead", "--to", "worker", "--force", "-m", "review",
                   "--work-item", "widget", *flags) == 0
    assert bus.messages_for("worker")[-1].meta["external_deliverable"] is expected


@pytest.mark.parametrize("key", ["assignee_model_vendors", "assignee_model_vendor"])
def test_vendor_metadata_reserved_on_cli_and_draft_publication(bus, tmp_path, monkeypatch, key):
    assert command(bus, "task", "--from", "lead", "--to", "worker", "--force", "-m", "work",
                   "--meta", key + "=openai") == 2
    opener = task(bus)
    original = reply_transport.echo_reply_correlation
    def injected(meta, **kwargs):
        original(meta, **kwargs)
        meta[key] = {"worker": "openai"} if key.endswith("vendors") else "openai"
    monkeypatch.setattr(reply_transport, "echo_reply_correlation", injected)
    draft = tmp_path / "reply.md"
    draft.write_text("done", encoding="utf-8")
    assert reply_transport.deliver_draft_reply(bus, agent="worker", record=opener.to_dict(), draft_path=draft) is None
    assert not bus.messages_for("lead")
