"""B6a gate isolation and bounded worker-owned board projection."""
import json

import pytest

from agenttalk import cli, gates, web
from agenttalk.store import Store

HEAD = "a" * 40


def gate(root, name="wb.a.c1.unit", **extra):
    return gates.set_gate(root, name=name, status="red", severity="blocker", scope="global",
                          actor="alpha", evidence_source="automation_ci", **extra)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    return s


def test_unscoped_and_nonboard_checks_ignore_both_board_gate_sources(store, capsys):
    gate(store.root)
    state = gates.load_gate_state(store.root)
    state["required_gates"] = ["wb.missing.c1.unit"]
    gates.gates_path(store.root).write_text(json.dumps(state), encoding="utf-8")
    for scope in (None, "release"):
        result = gates.check_gates(store.root, scope=scope)
        assert result["verdict"] == "GO" and result["required_gates"] == [] and result["gates"] == []
        assert result["warnings"]
    store.send(sender="beta", recipient="alpha", kind="question", body="test", meta={"request_id": "q-1"})
    assert cli.main(["--root", str(store.root), "check", "--for", "alpha", "--to-request", "q-1", "--gates"]) == 0
    assert "GO" in capsys.readouterr().out


@pytest.mark.parametrize("path", ["web", "cli"])
def test_both_attention_paths_ignore_board_red_but_keep_global_blocker(store, path):
    gate(store.root)
    def holds():
        rows = (web.build_attention(web.RootDescriptor(store=store, label="test"))["items"] if path == "web"
                else cli._collect_attention_items(store, for_agent=None, roster=["alpha", "beta"]))
        return [r for r in rows if r["source"] == ("gate" if path == "web" else "gate_hold")]
    assert holds() == []
    gate(store.root, name="release")
    assert len(holds()) == 1
    assert gates.check_gates(store.root)["verdict"] == "HOLD"


def test_root_requirement_refused_without_writing(store):
    with pytest.raises(ValueError, match="root.*required"):
        gate(store.root, required=True)
    assert not gates.gates_path(store.root).exists()
