"""#37 — publication-order sidecar self-heal + writer-skew diagnostics.

The store pins message publication order in a sidecar + a tamper-evidence anchor.
A version-skew bug (two writers reporting the same --version, one lacking
publication-order support) leaves valid message files on disk with no order
entry, which used to make EVERY send/ordered-read raise — a whole-team comms
mute. These tests cover the self-heal (fold orphans at the tail, prefix
preserved), the fail-loud paths that must NOT heal (genuine corruption), and the
Fix-2 doctor discriminator.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import doctor
from agenttalk.store import STORE_SCHEMA_CAPABILITIES, Store


# --------------------------------------------------------------------- helpers

def _order_path(store: Store) -> Path:
    return store.state_dir / "message-publication-order.json"


def _anchor_path(store: Store) -> Path:
    return store.state_dir / "message-publication-order.anchor.json"


def _read_order(store: Store) -> dict:
    return json.loads(_order_path(store).read_text(encoding="utf-8"))


def _read_anchor(store: Store) -> dict:
    return json.loads(_anchor_path(store).read_text(encoding="utf-8"))


def _send(store: Store, n: int, body: str = "m") -> None:
    for i in range(n):
        store.send(sender="alpha", recipient="beta", body=f"{body}{i}")


def _freeze_sidecar_behind(store: Store, drop: int) -> dict:
    """Rewrite the sidecar+anchor to FORGET the ``drop`` highest-sequence
    messages, simulating an order-less writer having appended them to disk
    without updating the sidecar. Returns the retained prefix map."""
    order = _read_order(store)
    keep = int(order["append_sequence"]) - drop
    kept = {mid: seq for mid, seq in order["messages"].items() if seq <= keep}
    _order_path(store).write_text(
        json.dumps({
            "schema_version": order["schema_version"],
            "append_sequence": keep,
            "messages": kept,
        }, indent=2),
        encoding="utf-8",
    )
    _anchor_path(store).write_text(
        json.dumps({
            "schema_version": order["schema_version"],
            "append_sequence": keep,
            "chain_digest": Store._message_publication_order_chain(kept),
        }, indent=2),
        encoding="utf-8",
    )
    return kept


# ------------------------------------------------------------------ write heal

def test_write_path_heals_orphans_instead_of_wedging(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    _send(store, 5)
    prefix = _freeze_sidecar_behind(store, drop=2)   # 2 orphans on disk
    n_before = int(_read_order(store)["append_sequence"])  # == 5 - 2

    with caplog.at_level("WARNING"):
        store.send(sender="alpha", recipient="beta", body="post-skew")  # must NOT raise

    order = _read_order(store)
    # 2 orphans folded + 1 new message
    assert order["append_sequence"] == n_before + 3
    # the anchored prefix is byte-for-byte preserved
    for mid, seq in prefix.items():
        assert order["messages"][mid] == seq
    # result is contiguous (so _validate accepts it)
    assert set(order["messages"].values()) == set(range(1, order["append_sequence"] + 1))
    assert "healed" in caplog.text.lower()


def test_healed_store_reanchors(store: Store) -> None:
    _send(store, 4)
    _freeze_sidecar_behind(store, drop=2)
    store.send(sender="alpha", recipient="beta", body="x")
    anchor = _read_anchor(store)
    order = _read_order(store)
    # anchor covers the full healed sidecar again
    assert anchor["append_sequence"] == order["append_sequence"]
    assert anchor["chain_digest"] == Store._message_publication_order_chain(order["messages"])


# ------------------------------------------------------------------- read heal

def test_read_path_heals_in_memory_without_writing(store: Store) -> None:
    _send(store, 5)
    _freeze_sidecar_behind(store, drop=2)
    order_before = _order_path(store).read_bytes()
    anchor_before = _anchor_path(store).read_bytes()

    ordered = store.publication_ordered_messages()

    assert len(ordered) == 5                         # all messages returned, no raise
    assert _order_path(store).read_bytes() == order_before   # a read never writes
    assert _anchor_path(store).read_bytes() == anchor_before


def test_read_and_write_folds_agree(store: Store) -> None:
    _send(store, 5)
    _freeze_sidecar_behind(store, drop=2)
    read_order = [m.id for m in store.publication_ordered_messages()]

    store.send(sender="alpha", recipient="beta", body="persist")  # write-heal persists

    persisted = _read_order(store)["messages"]
    # the pre-existing messages keep the exact relative order the read-heal gave
    assert sorted(read_order, key=lambda mid: persisted[mid]) == read_order


# ----------------------------------------------------------- fail-loud (tamper)

def test_tamper_below_anchor_fails_loud_and_does_not_heal(store: Store) -> None:
    _send(store, 4)
    order = _read_order(store)
    by_seq = sorted(order["messages"].items(), key=lambda kv: kv[1])
    id0, id1 = by_seq[0][0], by_seq[1][0]
    # swap two sequences: still contiguous (passes _validate) but the pinned
    # chain digest no longer matches -> genuine tamper, must fail loud.
    order["messages"][id0], order["messages"][id1] = (
        order["messages"][id1], order["messages"][id0],
    )
    _order_path(store).write_text(json.dumps(order), encoding="utf-8")

    with pytest.raises(ValueError, match="chain digest mismatch"):
        store.publication_ordered_messages()


def test_durable_anchor_ahead_of_sidecar_fails_loud(store: Store) -> None:
    _send(store, 3)
    anchor = _read_anchor(store)
    anchor["append_sequence"] = int(anchor["append_sequence"]) + 1  # sidecar lost history
    _anchor_path(store).write_text(json.dumps(anchor), encoding="utf-8")

    with pytest.raises(ValueError, match="anchor is ahead of its sidecar"):
        store.publication_ordered_messages()


def test_lone_anchor_missing_sidecar_fails_loud(store: Store) -> None:
    _send(store, 3)
    _order_path(store).unlink()   # sidecar gone, anchor remains
    with pytest.raises(ValueError, match="sidecar is missing while its anchor exists"):
        store.publication_ordered_messages()


# --------------------------------------------------------- anchor-absent (benign)

def test_anchor_absent_serves_reads_and_reanchors_on_write(store: Store) -> None:
    _send(store, 3)
    _anchor_path(store).unlink()   # crash-during-bootstrap or removed anchor
    # a read still works (must not wedge a benign crash-recovery)
    assert len(store.publication_ordered_messages()) == 3
    # the next write re-anchors
    store.send(sender="alpha", recipient="beta", body="x")
    assert _anchor_path(store).exists()
    order = _read_order(store)
    anchor = _read_anchor(store)
    assert anchor["append_sequence"] == order["append_sequence"]
    assert anchor["chain_digest"] == Store._message_publication_order_chain(order["messages"])


# --------------------------------------------------------------- idempotency

def test_heal_is_idempotent(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    _send(store, 4)
    _freeze_sidecar_behind(store, drop=2)
    store.send(sender="alpha", recipient="beta", body="heal")   # heals once
    with caplog.at_level("WARNING"):
        caplog.clear()
        store.send(sender="alpha", recipient="beta", body="again")  # nothing to heal
    assert "healed" not in caplog.text.lower()
    order = _read_order(store)
    assert set(order["messages"].values()) == set(range(1, order["append_sequence"] + 1))


# ------------------------------------------------------- Fix 2: discriminator

def test_extend_order_with_orphans_preserves_prefix_and_is_pure() -> None:
    order = {"schema_version": 1, "append_sequence": 3,
             "messages": {"a": 1, "b": 2, "c": 3}}
    healed = Store._extend_order_with_orphans(order, ["z", "y"])
    # original untouched (pure)
    assert order["append_sequence"] == 3 and order["messages"] == {"a": 1, "b": 2, "c": 3}
    # prefix preserved; orphans appended id-sorted at the tail
    assert healed["messages"]["a"] == 1 and healed["messages"]["b"] == 2
    assert healed["messages"]["c"] == 3
    assert healed["messages"]["y"] == 4 and healed["messages"]["z"] == 5
    assert healed["append_sequence"] == 5


def test_doctor_reports_module_path_and_capabilities(store_root: Path) -> None:
    report = doctor.run(store_root)
    data = report.to_dict()
    assert data["agenttalk_module_path"]
    assert data["agenttalk_module_path"].replace("\\", "/").endswith("agenttalk")
    assert "message-publication-order/v1" in data["store_schema_capabilities"]
    assert "message-publication-order/v1" in STORE_SCHEMA_CAPABILITIES


def _pub_order_check(report) -> object:
    return next(c for c in report.checks if c.name == "publication order")


def test_doctor_warns_on_absent_anchor(store: Store, store_root: Path) -> None:
    _send(store, 2)
    _anchor_path(store).unlink()
    check = _pub_order_check(doctor.run(store_root))
    assert check.status == "warn"
    assert "anchor is absent" in check.details


def test_doctor_errors_on_corrupt_order(store: Store, store_root: Path) -> None:
    _send(store, 3)
    anchor = _read_anchor(store)
    anchor["append_sequence"] = int(anchor["append_sequence"]) + 1  # anchor ahead
    _anchor_path(store).write_text(json.dumps(anchor), encoding="utf-8")
    check = _pub_order_check(doctor.run(store_root))
    assert check.status == "error"
    assert "integrity check failed" in check.details


def test_doctor_errors_on_lone_anchor(store: Store, store_root: Path) -> None:
    _send(store, 2)
    _order_path(store).unlink()   # sidecar gone, anchor remains
    check = _pub_order_check(doctor.run(store_root))
    assert check.status == "error"
    assert "missing while its tamper-anchor exists" in check.details


def test_doctor_ok_on_healthy_order(store: Store, store_root: Path) -> None:
    _send(store, 2)
    check = _pub_order_check(doctor.run(store_root))
    assert check.status == "ok"
