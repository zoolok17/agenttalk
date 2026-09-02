"""#55 slice-1 PR-A: common JSON envelope, strict duplicate-key reading, and
path safety (DESIGN-55-comprehension-plane.md, "Common JSON envelope").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agenttalk.comprehension import envelope as env
from agenttalk.comprehension.errors import EnvelopeError


# ----------------------------------------------------------- strict JSON reading

def test_strict_json_loads_rejects_duplicate_top_level_key() -> None:
    with pytest.raises(EnvelopeError, match="duplicate"):
        env.strict_json_loads('{"a": 1, "a": 2}')


def test_strict_json_loads_rejects_duplicate_nested_key() -> None:
    with pytest.raises(EnvelopeError, match="duplicate"):
        env.strict_json_loads('{"a": {"b": 1, "b": 2}}')


def test_strict_json_loads_accepts_well_formed_document() -> None:
    assert env.strict_json_loads('{"a": 1, "b": [1, 2, 3]}') == {"a": 1, "b": [1, 2, 3]}


def test_strict_json_loads_wraps_malformed_json() -> None:
    with pytest.raises(EnvelopeError):
        env.strict_json_loads("{not json")


def test_read_json_document_rejects_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_bytes(b"\xef\xbb\xbf" + b'{"a": 1}')
    with pytest.raises(EnvelopeError, match="byte-order mark"):
        env.read_json_document(path)


def test_read_json_document_rejects_duplicate_key(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    with pytest.raises(EnvelopeError, match="duplicate"):
        env.read_json_document(path)


def test_read_json_document_reads_a_well_formed_document(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    assert env.read_json_document(path) == {"a": 1}


def test_read_json_document_wraps_missing_file(tmp_path: Path) -> None:
    with pytest.raises(EnvelopeError, match="could not read"):
        env.read_json_document(tmp_path / "missing.json")


# ----------------------------------------------------------- RFC 3339 timestamps

def test_validate_rfc3339_utc_accepts_z_suffixed_timestamp() -> None:
    assert env.validate_rfc3339_utc("2026-08-26T09:15:30Z", label="generated_at") == \
        "2026-08-26T09:15:30Z"


def test_validate_rfc3339_utc_accepts_fractional_seconds() -> None:
    env.validate_rfc3339_utc("2026-08-26T09:15:30.123456Z", label="generated_at")


@pytest.mark.parametrize("value", [
    "2026-08-26T09:15:30+00:00",  # numeric offset, not the 'Z' spelling
    "2026-08-26T09:15:30",         # no zone at all
    "2026-08-26",                  # date-only
    "not-a-timestamp",
    "",
    None,
    12345,
])
def test_validate_rfc3339_utc_rejects_everything_else(value) -> None:
    with pytest.raises(EnvelopeError):
        env.validate_rfc3339_utc(value, label="generated_at")


# ----------------------------------------------------------- envelope validation

def _envelope(**overrides) -> dict:
    doc = {
        "schema_version": 1,
        "artifact_type": "agenttalk.comprehension.modules",
        "scan_id": "20260826T091530Z-a1b2c3d4",
        "generated_at": "2026-08-26T09:15:30Z",
    }
    doc.update(overrides)
    return doc


def test_validate_envelope_accepts_a_well_formed_envelope() -> None:
    doc = _envelope()
    assert env.validate_envelope(
        doc, artifact_type="agenttalk.comprehension.modules", schema_version=1,
    ) is doc


def test_validate_envelope_rejects_non_object() -> None:
    with pytest.raises(EnvelopeError, match="JSON object"):
        env.validate_envelope([1, 2], artifact_type="x", schema_version=1)


@pytest.mark.parametrize("field", ["schema_version", "artifact_type", "scan_id", "generated_at"])
def test_validate_envelope_rejects_missing_required_field(field: str) -> None:
    doc = _envelope()
    del doc[field]
    with pytest.raises(EnvelopeError, match="missing required"):
        env.validate_envelope(
            doc, artifact_type="agenttalk.comprehension.modules", schema_version=1,
        )


def test_validate_envelope_rejects_artifact_type_mismatch() -> None:
    doc = _envelope(artifact_type="agenttalk.comprehension.dependencies")
    with pytest.raises(EnvelopeError, match="artifact_type mismatch"):
        env.validate_envelope(
            doc, artifact_type="agenttalk.comprehension.modules", schema_version=1,
        )


def test_validate_envelope_rejects_a_higher_schema_version() -> None:
    doc = _envelope(schema_version=2)
    with pytest.raises(EnvelopeError, match="newer"):
        env.validate_envelope(
            doc, artifact_type="agenttalk.comprehension.modules", schema_version=1,
        )


def test_validate_envelope_rejects_an_older_schema_version() -> None:
    """No reader migration is registered in PR-A — an older version is
    reported, not silently accepted (design: readers "accept the exact
    version")."""
    doc = _envelope(schema_version=0)
    with pytest.raises(EnvelopeError, match="older"):
        env.validate_envelope(
            doc, artifact_type="agenttalk.comprehension.modules", schema_version=1,
        )


def test_validate_envelope_rejects_non_integer_schema_version() -> None:
    doc = _envelope(schema_version="1")
    with pytest.raises(EnvelopeError, match="integer"):
        env.validate_envelope(
            doc, artifact_type="agenttalk.comprehension.modules", schema_version=1,
        )


def test_validate_envelope_rejects_empty_scan_id() -> None:
    doc = _envelope(scan_id="")
    with pytest.raises(EnvelopeError, match="scan_id"):
        env.validate_envelope(
            doc, artifact_type="agenttalk.comprehension.modules", schema_version=1,
        )


# ----------------------------------------------------------- path safety

@pytest.mark.parametrize("value", [
    "src/legacy/Service.java",
    "a",
    "deeply/nested/path/to/file.ext",
])
def test_validate_relative_path_accepts_well_formed_paths(value: str) -> None:
    assert env.validate_relative_path(value) == value


@pytest.mark.parametrize("value,match", [
    ("", "non-empty"),
    (None, "non-empty"),
    (123, "non-empty"),
    ("a\x00b", "NUL"),
    ("a\\b", "POSIX"),
    ("/etc/passwd", "absolute"),
    ("C:/Windows/System32", "absolute"),
    ("https://example.invalid/x", "URL-like"),
    ("git://example.invalid/x", "URL-like"),
    ("../escape", "'..'"),
    ("a/../b", "'..'"),
    ("a//b", "empty or '.'"),
    ("a/./b", "empty or '.'"),
])
def test_validate_relative_path_rejects_unsafe_values(value, match: str) -> None:
    with pytest.raises(EnvelopeError, match=match):
        env.validate_relative_path(value)


def test_resolve_under_root_accepts_a_path_inside_the_root(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("x", encoding="utf-8")
    resolved = env.resolve_under_root("src/a.txt", root=tmp_path)
    assert resolved == (tmp_path / "src" / "a.txt").resolve()


def test_resolve_under_root_rejects_a_path_that_escapes_via_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        # CAVEAT (reviewer-3 F-3 on PR-A, rq-5bd5427ad64d): on Windows, an
        # unprivileged process can only create a symlink with Developer
        # Mode enabled (or SeCreateSymbolicLinkPrivilege granted) — without
        # it, symlink_to() raises OSError [WinError 1314] and this test
        # SKIPS. C-1 / #213 (PR-B fix round): conftest.py's session-scoped
        # `_enable_windows_symlink_creation_without_elevation` fixture now
        # enables Developer Mode on hosted Windows CI runners (which run
        # elevated already), so this executes there instead of skipping.
        # This branch only still fires on a genuinely non-elevated local
        # dev machine, where that fixture's registry write itself fails
        # silently and this remains a graceful local skip.
        pytest.skip("symlink creation is not permitted in this environment")
    with pytest.raises(EnvelopeError, match="outside the project root"):
        env.resolve_under_root("escape/x", root=root)


def test_resolve_under_root_rejects_syntactically_unsafe_paths(tmp_path: Path) -> None:
    with pytest.raises(EnvelopeError, match="'..'"):
        env.resolve_under_root("../x", root=tmp_path)


# ----------------------------------------------------------- case-fold collisions

def test_find_case_fold_collisions_finds_a_colliding_pair() -> None:
    collisions = env.find_case_fold_collisions(["src/Foo.java", "src/foo.java", "src/bar.java"])
    assert collisions == [("src/Foo.java", "src/foo.java")]


def test_find_case_fold_collisions_is_empty_for_distinct_paths() -> None:
    assert env.find_case_fold_collisions(["src/Foo.java", "src/Bar.java"]) == []


def test_find_case_fold_collisions_ignores_exact_duplicates() -> None:
    """An exact duplicate path is a different problem (a caller-side bug or
    a record listed twice); this helper only reports a CASE-FOLD collision
    between two DISTINCT spellings."""
    assert env.find_case_fold_collisions(["src/Foo.java", "src/Foo.java"]) == []


def test_find_case_fold_collisions_finds_an_nfc_nfd_normalization_variant_pair() -> None:
    """FIX ROUND 36 (thirtieth cold read, F4 MAJOR, completeness, .cr30-
    uni verbatim): a bare casefold() never normalizes composition - a
    precomposed 'é' (U+00E9, NFC) and 'e' + a combining acute accent
    (U+0065 U+0301, NFD) casefold to DIFFERENT strings even though they
    render as the visually identical name, the exact ambiguity this
    detector exists to catch (platform_identity's own unicode_
    normalizing: false already admits both forms coexist). Unit-tested
    directly against the detector, per the reader's own caveat: a real
    filesystem's own case-fold/normalization behavior (NTFS especially)
    is not something this test relies on or asserts about."""
    import unicodedata

    nfc = unicodedata.normalize("NFC", "src/Café.java")
    nfd = unicodedata.normalize("NFD", "src/Café.java")
    assert nfc != nfd  # the two spellings really are distinct code-point sequences
    collisions = env.find_case_fold_collisions([nfc, nfd, "src/bar.java"])
    assert collisions == [(nfc, nfd)]


def test_find_case_fold_collisions_plain_ascii_control_is_unaffected() -> None:
    """Companion control: an ordinary plain-ASCII case-fold collision
    (no Unicode normalization variance involved at all) is unaffected by
    the widened NFC-normalizing key."""
    collisions = env.find_case_fold_collisions(["src/Foo.java", "src/foo.java"])
    assert collisions == [("src/Foo.java", "src/foo.java")]


def test_is_pure_case_fold_collision_distinguishes_the_two_causes() -> None:
    """FIX ROUND 36 (F4 MAJOR): the per-pair cause check a caller needs
    to publish a TRUTHFUL detail - "case-folds identically" is true for
    an ordinary case-only pair, but FALSE for a pair that only collides
    once Unicode-normalized (nothing differs by case at all)."""
    import unicodedata

    nfc = unicodedata.normalize("NFC", "Café.java")
    nfd = unicodedata.normalize("NFD", "Café.java")
    assert env.is_pure_case_fold_collision("Foo.java", "foo.java") is True
    assert env.is_pure_case_fold_collision(nfc, nfd) is False
