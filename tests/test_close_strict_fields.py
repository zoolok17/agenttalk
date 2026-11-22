"""Damaged close fields must not waive evidence or impersonate an artifact."""

import json

import pytest

from agenttalk import cli, close, printed_commands as pc
from agenttalk.store import Store
from test_close import _gate_go, _satisfied


@pytest.mark.parametrize("value", ["false", 0, 1, None, []])
def test_bad_isolation_field_holds_and_names_repair(value):
    record = dict(_satisfied(), non_lane_isolation_not_asserted=value)
    result = close.compute_verdict(record, _gate_go())
    assert result["verdict"] == "HOLD"
    detail = " ".join(h["detail"] for h in result["holds"])
    assert "non_lane_isolation_not_asserted" in detail and "new close" in detail
    assert pc.shown(value if isinstance(value, str) else json.dumps(value), limit=60) in detail
    assert cli._close_worktree_eval(None, record)["status"] != "not_applicable"


def test_missing_isolation_field_does_not_waive_release():
    record = _satisfied()
    record.pop("non_lane_isolation_not_asserted")
    result = close.compute_verdict(record, _gate_go())
    assert result["verdict"] == "HOLD"
    assert any(h["code"] == close.HOLD_WORKTREE_ISOLATION and "verified" in h["detail"] for h in result["holds"])
    assert all("non_lane_isolation_not_asserted" not in h["detail"] for h in result["holds"])


def test_legacy_record_without_isolation_keeps_docs_verdict(tmp_path):
    store = Store(tmp_path)
    store.init(["lead"])
    record = _satisfied()
    record["scope"] = record["gate_scope"] = "docs"
    record.pop("non_lane_isolation_not_asserted")
    close.create_close(store, record)
    loaded = close.load_close(store, record["close_id"])
    assert "non_lane_isolation_not_asserted" not in loaded
    assert close.compute_verdict(loaded, _gate_go())["verdict"] == "GO"


@pytest.mark.parametrize("value", [True, False])
def test_boolean_isolation_values_keep_their_meaning(value):
    record = dict(_satisfied(), non_lane_isolation_not_asserted=value)
    result = close.compute_verdict(record, _gate_go())
    assert result["ok"] is value


@pytest.mark.parametrize("value", [0, 1, [], ["pointer"], False, True])
def test_bad_dirty_artifact_holds_and_names_repair(value):
    record = dict(_satisfied(), revision_clean=False, dirty_artifact=value)
    result = close.compute_verdict(record, _gate_go())
    assert result["verdict"] == "HOLD"
    detail = " ".join(h["detail"] for h in result["holds"])
    assert "dirty_artifact" in detail and "new close" in detail
    assert pc.shown(value if isinstance(value, str) else json.dumps(value), limit=60) in detail


def test_missing_dirty_artifact_holds():
    record = _satisfied()
    record.pop("dirty_artifact")
    result = close.compute_verdict(record, _gate_go())
    assert result["verdict"] == "HOLD"
    assert any("dirty_artifact" in h["detail"] and "missing" in h["detail"] for h in result["holds"])


def test_bad_dirty_artifact_holds_even_for_a_clean_revision():
    result = close.compute_verdict(dict(_satisfied(), dirty_artifact=1), _gate_go())
    assert result["verdict"] == "HOLD"


@pytest.mark.parametrize("value", [None, "false", "pointer:diff"])
def test_valid_dirty_artifact_types_keep_their_meaning(value):
    record = dict(_satisfied(), revision_clean=False, dirty_artifact=value)
    result = close.compute_verdict(record, _gate_go())
    assert result["ok"] is (value is not None)
