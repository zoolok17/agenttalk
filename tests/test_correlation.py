"""Tests for the #17 correlation resolvers.

Drafted by the wrapped Qwen worker (qwen-dev-1) over the agenttalk bus; adapted
to the package import path and reviewed/landed by the lead. Covers all three
record shapes (real on-disk message with request_id in meta, normalized recv
record with it top-level, correlation_id-only) plus the broadcast alias and the
guard that distinct *_request_id keys are never folded in.
"""

from agenttalk.correlation import (
    resolve_broadcast_id,
    resolve_correlation_id,
    resolve_request_id,
)


class TestResolveRequestId:
    def test_real_on_disk_message_shape(self):
        # Real on-disk message: meta.request_id only, no top-level request_id.
        record = {"id": "msg-123", "meta": {"request_id": "q-1"}}
        assert resolve_request_id(record) == "q-1"

    def test_normalized_recv_record(self):
        # Normalized recv record: both top-level and meta have request_id.
        record = {"request_id": "q-2", "meta": {"request_id": "q-2"}}
        assert resolve_request_id(record) == "q-2"

    def test_correlation_id_only(self):
        assert resolve_request_id({"correlation_id": "q-3"}) == "q-3"

    def test_empty_record(self):
        assert resolve_request_id({}) is None

    def test_empty_meta(self):
        assert resolve_request_id({"meta": {}}) is None

    def test_broadcast_message(self):
        # Broadcast: request_id == broadcast_id, both in meta.
        record = {"meta": {"request_id": "b-9", "broadcast_id": "b-9"}}
        assert resolve_request_id(record) == "b-9"
        assert resolve_broadcast_id(record) == "b-9"

    def test_guard_does_not_read_origin_or_forwarded(self):
        # Must NOT resolve origin_request_id / forwarded_request_id.
        record = {"meta": {"origin_request_id": "esc-x", "forwarded_request_id": "f-y"}}
        assert resolve_request_id(record) is None


class TestResolveBroadcastId:
    def test_top_level_broadcast_id(self):
        assert resolve_broadcast_id({"broadcast_id": "b-1"}) == "b-1"

    def test_meta_broadcast_id(self):
        assert resolve_broadcast_id({"meta": {"broadcast_id": "b-2"}}) == "b-2"

    def test_top_level_preferred_over_meta(self):
        record = {"broadcast_id": "b-top", "meta": {"broadcast_id": "b-meta"}}
        assert resolve_broadcast_id(record) == "b-top"

    def test_no_broadcast_id(self):
        assert resolve_broadcast_id({}) is None


class TestResolveCorrelationId:
    def test_request_id_takes_precedence(self):
        record = {"request_id": "q-1", "broadcast_id": "b-1"}
        assert resolve_correlation_id(record) == "q-1"

    def test_broadcast_id_when_no_request_id(self):
        assert resolve_correlation_id({"broadcast_id": "b-1"}) == "b-1"

    def test_no_correlation_id(self):
        assert resolve_correlation_id({}) is None
