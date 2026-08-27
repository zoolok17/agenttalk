"""#55 slice-1 PR-A: exact-byte vs. canonical-content digests
(DESIGN-55-comprehension-plane.md, "Common JSON envelope").

The headline acceptance fixture from the PR-A dispatch lives here: two
byte-identical source scans must produce different scan IDs but equal
content digests.
"""

from __future__ import annotations

from pathlib import Path

from agenttalk.comprehension import digests as dg


# ----------------------------------------------------------- canonical JSON bytes

def test_canonical_json_bytes_sorts_keys() -> None:
    assert dg.canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_canonical_json_bytes_is_compact() -> None:
    assert b" " not in dg.canonical_json_bytes({"a": [1, 2, 3]})


def test_canonical_json_bytes_preserves_non_ascii() -> None:
    assert dg.canonical_json_bytes({"a": "café"}) == '{"a":"café"}'.encode("utf-8")


# ----------------------------------------------------------- exact-byte digests

def test_sha256_bytes_is_deterministic() -> None:
    assert dg.sha256_bytes(b"hello") == dg.sha256_bytes(b"hello")
    assert dg.sha256_bytes(b"hello") != dg.sha256_bytes(b"hellp")


def test_sha256_file_matches_sha256_bytes(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"the quick brown fox")
    assert dg.sha256_file(path) == dg.sha256_bytes(b"the quick brown fox")


def test_sha256_file_streams_large_content(tmp_path: Path) -> None:
    path = tmp_path / "big.bin"
    chunk = b"x" * (2 * 1024 * 1024)  # bigger than the internal 1 MiB read chunk
    path.write_bytes(chunk)
    assert dg.sha256_file(path) == dg.sha256_bytes(chunk)


# ----------------------------------------------------------- canonical content digest

def _fake_scan(scan_id: str, generated_at: str, capture_time: str) -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "agenttalk.comprehension.modules",
        "scan_id": scan_id,
        "generated_at": generated_at,
        "units": [
            {
                "unit_id": "u1",
                "kind": "file",
                "paths": ["src/A.java"],
                "producers": [
                    {"adapter_id": "java", "adapter_version": "1", "capture_time": capture_time},
                ],
            },
        ],
    }


def test_two_byte_identical_scans_have_different_scan_ids_but_equal_content_digest() -> None:
    """The headline PR-A acceptance fixture (dispatch: 'two byte-identical
    source scans -> different scan IDs, equal content digests')."""
    first = _fake_scan(
        "20260826T091530Z-a1b2c3d4", "2026-08-26T09:15:30Z", "2026-08-26T09:15:30Z")
    second = _fake_scan(
        "20260826T091545Z-e5f6a7b8", "2026-08-26T09:15:45Z", "2026-08-26T09:15:45Z")
    assert first["scan_id"] != second["scan_id"]
    assert dg.canonical_content_digest(first) == dg.canonical_content_digest(second)


def test_canonical_content_digest_still_differs_on_a_real_content_change() -> None:
    first = _fake_scan(
        "20260826T091530Z-a1b2c3d4", "2026-08-26T09:15:30Z", "2026-08-26T09:15:30Z")
    second = _fake_scan(
        "20260826T091545Z-e5f6a7b8", "2026-08-26T09:15:45Z", "2026-08-26T09:15:45Z")
    second["units"][0]["paths"] = ["src/B.java"]
    assert dg.canonical_content_digest(first) != dg.canonical_content_digest(second)


def test_canonical_content_digest_strips_generation_identity_at_any_depth() -> None:
    nested_only_difference = {
        "a": 1,
        "nested": {"capture_time": "2026-08-26T09:15:30Z", "value": "x"},
    }
    other_capture_time = {
        "a": 1,
        "nested": {"capture_time": "2026-08-26T10:00:00Z", "value": "x"},
    }
    assert dg.canonical_content_digest(nested_only_difference) == \
        dg.canonical_content_digest(other_capture_time)


def test_canonical_content_digest_respects_a_caller_supplied_strip_set() -> None:
    doc_a = {"a": 1, "custom_generation_field": "one"}
    doc_b = {"a": 1, "custom_generation_field": "two"}
    assert dg.canonical_content_digest(doc_a) != dg.canonical_content_digest(doc_b)
    wider = frozenset(dg.GENERATION_IDENTITY_KEYS | {"custom_generation_field"})
    assert dg.canonical_content_digest(doc_a, strip_keys=wider) == \
        dg.canonical_content_digest(doc_b, strip_keys=wider)


# ----------------------------------------------------------- run-level content digest

def test_run_content_digest_is_deterministic_and_order_sensitive() -> None:
    artifacts = [
        {"artifact_type": "modules", "schema_version": 1, "record_count": 3,
         "content_digest": "aaa"},
        {"artifact_type": "dependencies", "schema_version": 1, "record_count": 5,
         "content_digest": "bbb"},
    ]
    reordered = list(reversed(artifacts))
    assert dg.run_content_digest(artifacts) == dg.run_content_digest(list(artifacts))
    assert dg.run_content_digest(artifacts) != dg.run_content_digest(reordered)


def test_run_content_digest_changes_when_an_artifact_digest_changes() -> None:
    artifacts = [
        {"artifact_type": "modules", "schema_version": 1, "record_count": 3,
         "content_digest": "aaa"},
    ]
    changed = [
        {"artifact_type": "modules", "schema_version": 1, "record_count": 3,
         "content_digest": "ccc"},
    ]
    assert dg.run_content_digest(artifacts) != dg.run_content_digest(changed)


# ----------------------------------------------------------- root binding digest

def test_root_binding_digest_is_deterministic() -> None:
    assert dg.root_binding_digest("/home/dev/project") == dg.root_binding_digest("/home/dev/project")


def test_root_binding_digest_differs_for_different_roots() -> None:
    assert dg.root_binding_digest("/home/dev/project-a") != \
        dg.root_binding_digest("/home/dev/project-b")


def test_root_binding_digest_never_leaks_the_input_verbatim() -> None:
    digest = dg.root_binding_digest("/home/dev/super-secret-project-name")
    assert "super-secret-project-name" not in digest
    assert len(digest) == 64  # hex-encoded sha256


# ----------------------------------------------------------- deterministic record IDs

def test_unit_id_is_deterministic_and_path_order_independent() -> None:
    a = dg.unit_id(kind="file", paths=["b.java", "a.java"], qualified_name=None)
    b = dg.unit_id(kind="file", paths=["a.java", "b.java"], qualified_name=None)
    assert a == b


def test_unit_id_differs_by_kind_path_or_qualified_name() -> None:
    base = dg.unit_id(kind="file", paths=["a.java"], qualified_name=None)
    assert base != dg.unit_id(kind="component", paths=["a.java"], qualified_name=None)
    assert base != dg.unit_id(kind="file", paths=["b.java"], qualified_name=None)
    assert base != dg.unit_id(kind="file", paths=["a.java"], qualified_name="p.A")


def test_edge_id_is_deterministic() -> None:
    a = dg.edge_id(from_unit_id="u1", relation="import", target="java.util.List", phase="runtime")
    b = dg.edge_id(from_unit_id="u1", relation="import", target="java.util.List", phase="runtime")
    assert a == b


def test_edge_id_differs_by_relation() -> None:
    a = dg.edge_id(from_unit_id="u1", relation="import", target="x", phase="runtime")
    b = dg.edge_id(from_unit_id="u1", relation="invoke", target="x", phase="runtime")
    assert a != b


def test_entry_point_id_is_deterministic() -> None:
    a = dg.entry_point_id(kind="cli_main", owning_unit_id="u1", name="main")
    b = dg.entry_point_id(kind="cli_main", owning_unit_id="u1", name="main")
    assert a == b


def test_feature_id_is_unit_order_independent() -> None:
    a = dg.feature_id(label="checkout", unit_ids=["u2", "u1"])
    b = dg.feature_id(label="checkout", unit_ids=["u1", "u2"])
    assert a == b


def test_signal_id_differs_by_policy_version() -> None:
    a = dg.signal_id(unit_id="u1", check="deps_resolved", policy_version=1)
    b = dg.signal_id(unit_id="u1", check="deps_resolved", policy_version=2)
    assert a != b


def test_conflict_id_is_claim_order_independent() -> None:
    a = dg.conflict_id(conflict_kind="unit_kind", anchor="p.Foo", claim_digests=["d2", "d1"])
    b = dg.conflict_id(conflict_kind="unit_kind", anchor="p.Foo", claim_digests=["d1", "d2"])
    assert a == b
