"""Operator-configured vendors and exact escalation-to-item correlation."""
import json

import pytest

from agenttalk import attention, web, work_tags
from agenttalk.store import Store
from test_work_tags import bus as bus_fixture, command, external

bus = bus_fixture


@pytest.mark.parametrize("vendor", ["anthropic", "openai", "alibaba", "other", "unverified"])
def test_operator_vendor_config_and_single_dispatch(bus, vendor):
    assert command(bus, "roster", "set-model-vendor", "worker", vendor) == 0
    assert bus.load_config()["model_vendor"] == {"worker": vendor}
    msg = bus.send(sender="lead", recipient="worker", kind="task", body="build")
    assert msg.meta["assignee_model_vendors"] == {"worker": vendor}
    assert command(bus, "roster", "set-model-vendor", "worker", "--clear") == 0
    assert bus.valid_messages()[0].meta["assignee_model_vendors"] == {"worker": vendor}


@pytest.mark.parametrize("mapping", [{"ghost": "openai"}, {"worker": "qwen"}, [], {"worker": 1}])
def test_invalid_vendor_config_refused(bus, mapping):
    cfg = bus.load_config()
    cfg["model_vendor"] = mapping
    bus.config_path.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="model_vendor"):
        bus.load_config()


def test_group_map_frozen_across_membership_and_vendor_change(bus, monkeypatch):
    (bus.dir / "supervisor.json").write_text(json.dumps({"agents": {
        "worker": {"cli": "claude", "model": "qwen3-coder"}}}), encoding="utf-8")
    bus.set_model_vendor("worker", "alibaba")  # qwen via claude transport
    bus.set_model_vendor("reviewer", "openai")
    bus.set_group("builders", ["worker", "reviewer"])
    original = Store.send
    def changing(self, **kwargs):
        msg = original(self, **kwargs)
        self.set_group("builders", ["lead"])
        self.set_model_vendor("worker", "anthropic")
        return msg
    monkeypatch.setattr(Store, "send", changing)
    assert command(bus, "broadcast", "--from", "lead", "--to-group", "builders", "--kind", "task", "--force",
                   "--meta", "work_item=widget", "--meta", "stage=build", "-m", "build") == 0
    copies = bus.valid_messages()
    assert {m.recipient for m in copies} == {"worker", "reviewer"}
    assert len({m.meta["request_id"] for m in copies}) == 1
    assert all(m.meta["assignee_model_vendors"] == {"worker": "alibaba", "reviewer": "openai"} for m in copies)


def test_unknown_vendor_not_inferred_and_spoofs_refused(bus):
    msg = bus.send(sender="lead", recipient="worker", kind="review-request", body="review")
    assert msg.meta["assignee_model_vendors"] == {"worker": "unverified"}
    for key in ("assignee_model_vendor", "assignee_model_vendors"):
        assert command(bus, "broadcast", "--from", "lead", "--all", "--kind", "task", "--force", "-m", "build",
                       "--meta", key + "=anthropic") == 2
    assert len(bus.valid_messages()) == 1


def test_partial_group_resume_preserves_frozen_vendor_map(bus, monkeypatch):
    bus.set_model_vendor("worker", "alibaba")
    original = Store.send
    calls = []
    def partial(self, **kwargs):
        calls.append(kwargs["recipient"])
        if len(calls) == 2:
            raise OSError("interrupted")
        return original(self, **kwargs)
    monkeypatch.setattr(Store, "send", partial)
    assert command(bus, "broadcast", "--from", "lead", "--all", "--kind", "task", "--force", "-m", "build") == 5
    first = bus.valid_messages()[0]
    monkeypatch.setattr(Store, "send", original)
    bus.set_model_vendor("worker", "openai")
    assert command(bus, "broadcast", "--from", "lead", "--resume", first.meta["broadcast_id"]) == 0
    assert len(bus.valid_messages()) == 2
    assert all(m.meta["assignee_model_vendors"] == first.meta["assignee_model_vendors"] for m in bus.valid_messages())


def test_escalation_exact_item_cycle_in_sanitized_refs(bus):
    for item, cycle in (("widget", "02"), ("other", "3")):
        assert command(bus, "escalate", "--from", "worker", "-m", "private prompt", "--work-item", item,
                       "--work-cycle", cycle) == 0
    assert command(bus, "escalate", "--from", "worker", "-m", "unlinked") == 0
    payload = web.build_attention(web.RootDescriptor(bus, "test"), agents=[])
    linked = {m.meta["request_id"]: m.meta for m in bus.valid_messages()}
    items = [i for i in payload["items"] if i["source"] == "escalation"]
    assert len(items) == 3
    for item in items:
        ref = item["source_refs"][0]
        meta = linked[ref["request_id"]]
        assert ref == {"kind": "message", "request_id": meta["request_id"],
                       **({"work_item": meta["work_item"], "work_cycle": meta["work_cycle"]}
                          if "work_item" in meta else {})}
    assert "private prompt" not in json.dumps(payload)


@pytest.mark.parametrize("flags", [["--work-item", "Bad"], ["--work-item", "widget", "--work-cycle", "0"],
                                  ["--work-item", "widget", "--meta", "work_item=other"]])
def test_escalation_invalid_or_conflicting_tags_refuse(bus, flags):
    assert command(bus, "escalate", "--from", "worker", "-m", "decision", *flags) == 2
    assert not bus.valid_messages()


def test_malformed_external_check_json_has_same_refusal(bus):
    errors = []
    for gates in ("{", "{}"):
        with pytest.raises(ValueError) as exc:
            bus.send(sender="lead", recipient="worker", kind="task", body="review",
                     meta=dict(external(), work_item="widget", required_gates=gates))
        errors.append(str(exc.value))
    assert errors[0] == errors[1]


def test_legacy_invalid_escalation_link_does_not_escape_sanitizer():
    items = attention.needs_operator_items([{"request_id": "esc-one", "meta": {"work_item": "../private"}}])
    assert items[0]["source_refs"] == [{"kind": "message", "request_id": "esc-one"}]


def test_vendor_map_exact_recipient_validation():
    with pytest.raises(ValueError):
        work_tags.validate_vendor_map({"worker": "openai", "extra": "other"}, ["worker"])


@pytest.mark.parametrize("operation", ["remove", "retire", "rename"])
def test_vendor_config_lifecycle_keeps_historical_snapshot(bus, operation):
    bus.set_model_vendor("worker", "alibaba")
    sent = bus.send(sender="lead", recipient="worker", kind="review-request", body="review")
    if operation == "remove":
        bus.remove_agent("worker")
    elif operation == "retire":
        bus.retire_agent("worker")
    else:
        bus.rename_agent("worker", "worker-next")
    mapping = bus.load_config().get("model_vendor", {})
    assert mapping == ({"worker-next": "alibaba"} if operation == "rename" else {})
    retained = json.loads((bus.messages_dir / (sent.id + ".json")).read_text(encoding="utf-8"))
    assert retained["meta"]["assignee_model_vendors"] == {"worker": "alibaba"}


def test_group_review_dispatch_and_task_authority_guards(bus):
    assert command(bus, "broadcast", "--from", "worker", "--all", "--kind", "task",
                   "--force", "-m", "not authorized") == 2
    assert command(bus, "broadcast", "--from", "lead", "--all", "--kind", "task", "-m", "old readers") == 2
    assert command(bus, "broadcast", "--from", "lead", "--all", "--kind", "review-request", "-m", "review") == 0
    assert all(m.meta["assignee_model_vendors"] == {"worker": "unverified", "reviewer": "unverified"}
               for m in bus.valid_messages())


def test_escalation_equivalent_metadata_and_legacy_cycle(bus):
    assert command(bus, "escalate", "--from", "worker", "-m", "decision", "--meta", "work_item=widget") == 0
    items = web.build_attention(web.RootDescriptor(bus, "test"), agents=[])["items"]
    ref = next(i for i in items if i["source"] == "escalation")["source_refs"][0]
    assert (ref["work_item"], ref["work_cycle"]) == ("widget", "1")


def test_resume_refuses_conflicting_fanout_vendor_maps(bus, monkeypatch):
    assert command(bus, "broadcast", "--from", "lead", "--all", "--kind", "review-request", "-m", "review") == 0
    copies = bus.valid_messages()
    copies[1].meta["assignee_model_vendors"]["worker"] = "other"
    monkeypatch.setattr(Store, "valid_messages", lambda self: copies)
    assert command(bus, "broadcast", "--from", "lead", "--resume", copies[0].meta["broadcast_id"]) == 2
