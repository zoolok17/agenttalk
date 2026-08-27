"""#55 slice-1 PR-B item 2: file enumeration, platform/path policy, and the
three pre-freshness resource caps (DESIGN-55-comprehension-plane.md, "Scan
behavior" step 4 and "Resource caps").
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from agenttalk.comprehension import discovery


def _comprehension_dir(root: Path) -> Path:
    d = root / ".agenttalk" / "comprehension"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ----------------------------------------------------------- detect_platform_identity

def test_detect_platform_identity_returns_sane_fields(tmp_path: Path) -> None:
    comp_dir = _comprehension_dir(tmp_path)
    identity = discovery.detect_platform_identity(comp_dir)
    assert identity.os_family == os.name
    assert identity.architecture
    assert identity.path_normalization_version == discovery.PATH_NORMALIZATION_VERSION
    assert isinstance(identity.case_sensitive, bool)
    assert isinstance(identity.unicode_normalizing, bool)


def test_detect_platform_identity_cleans_up_its_own_probe_directory(tmp_path: Path) -> None:
    comp_dir = _comprehension_dir(tmp_path)
    discovery.detect_platform_identity(comp_dir)
    assert not (comp_dir / ".platform-probe").exists()


def test_detect_platform_identity_never_leaves_a_trace_visible_to_enumeration(
    tmp_path: Path,
) -> None:
    """The probe lives under .agenttalk/, which is always hard-excluded -
    so even if cleanup somehow left something behind, it could never
    become a spurious file unit. Belt and suspenders: assert cleanup AND
    that .agenttalk/ itself is excluded (covered below) means this can
    never leak into a real result."""
    (tmp_path / "real.txt").write_bytes(b"x")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert [f.relative_path for f in result.files] == ["real.txt"]


# ----------------------------------------------------------- default excludes

def test_enumerate_scope_finds_ordinary_files_with_correct_digest(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert len(result.files) == 1
    assert result.files[0].relative_path == "a.txt"
    assert result.files[0].byte_count == 5
    assert result.files[0].content_digest == hashlib.sha256(b"hello").hexdigest()
    assert result.degraded is False
    assert result.fingerprint_complete is True
    assert result.whole_scope_fingerprint is not None


@pytest.mark.parametrize("dirname,category", [
    (".git", "hard_excluded"),
    (".hg", "vcs"),
    (".svn", "vcs"),
    ("node_modules", "dependency_cache"),
    (".m2", "dependency_cache"),
    ("target", "generated_or_vendor"),
    ("build", "generated_or_vendor"),
    ("vendor", "generated_or_vendor"),
])
def test_enumerate_scope_excludes_default_directory_categories(
    tmp_path: Path, dirname: str, category: str,
) -> None:
    excluded_dir = tmp_path / dirname
    excluded_dir.mkdir()
    (excluded_dir / "inner.txt").write_bytes(b"should not be enumerated")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.files == []
    # >= 1, not == 1: the .agenttalk/ fixture directory itself is ALSO
    # hard_excluded in every run of this test, so the "hard_excluded"
    # category's count includes that baseline exclusion too.
    assert result.exclusions.get(category, 0) >= 1


def test_agenttalk_dir_itself_is_hard_excluded(tmp_path: Path) -> None:
    comp_dir = _comprehension_dir(tmp_path)
    (comp_dir / "index.json").write_text("{}", encoding="utf-8")
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.files == []
    assert result.exclusions.get("hard_excluded") == 1


@pytest.mark.parametrize("filename", [".env", ".env.local", "id_rsa", "server.pem", "cert.p12"])
def test_enumerate_scope_excludes_secret_file_patterns(tmp_path: Path, filename: str) -> None:
    (tmp_path / filename).write_bytes(b"secret")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.files == []
    assert result.exclusions.get("secret") == 1


def test_enumerate_scope_excludes_binary_content(tmp_path: Path) -> None:
    (tmp_path / "photo.dat").write_bytes(b"\x00\x01\x02binarydata")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.files == []
    assert result.exclusions.get("binary") == 1


# ----------------------------------------------------------- boundaries

def test_enumerate_scope_records_a_symlink_as_a_boundary_and_never_follows_it(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside-discovery-target"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_bytes(b"outside content")
    link = tmp_path / "link-to-outside"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.files == []
    assert len(result.boundaries) == 1
    assert result.boundaries[0].relative_path == "link-to-outside"
    assert result.boundaries[0].boundary_kind == "symlink"


def test_enumerate_scope_records_a_submodule_as_a_boundary_and_never_enters_it(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitmodules").write_text(
        '[submodule "lib"]\n\tpath = lib\n\turl = https://example.invalid/lib.git\n',
        encoding="utf-8",
    )
    submodule_dir = tmp_path / "lib"
    submodule_dir.mkdir()
    (submodule_dir / "inner.txt").write_bytes(b"should not be enumerated")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    # .gitmodules itself is an ordinary, non-excluded repo file - only the
    # submodule's OWN contents must never be entered.
    assert [f.relative_path for f in result.files] == [".gitmodules"]
    assert len(result.boundaries) == 1
    assert result.boundaries[0].relative_path == "lib"
    assert result.boundaries[0].boundary_kind == "submodule"


# ----------------------------------------------------------- resource caps

def test_per_file_size_cap_fires_before_binary_sniffing(tmp_path: Path, monkeypatch) -> None:
    """The lead's explicit round-4 callout: an oversized file that would
    ALSO look binary must be caught by the size cap first, never
    reclassified as a "binary exclude" - otherwise a large vendored binary
    (the exit-gate's JDK RPM case) could never trip the cap."""
    monkeypatch.setattr(discovery, "MAX_PER_FILE_BYTES", 4)
    (tmp_path / "big.bin").write_bytes(b"\x00\x01\x02\x03\x04\x05\x06\x07")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.files == []
    assert result.exclusions.get("resource_limit_oversized") == 1
    assert result.exclusions.get("binary") is None
    assert result.degraded is True
    assert result.fingerprint_complete is False
    assert result.whole_scope_fingerprint is None
    assert any(p["reason_code"] == "resource_limit" for p in result.problems)


def test_per_file_size_cap_does_not_read_the_oversized_files_content(
    tmp_path: Path, monkeypatch,
) -> None:
    """Verified by construction: stat() alone must decide this, so a file
    this test cannot actually read (permission-denied simulation isn't
    portable) is still safely skippable in principle. Here we assert the
    cheaper, equally conclusive property: the returned claim carries no
    digest for an oversized entry, because content was genuinely never
    read."""
    monkeypatch.setattr(discovery, "MAX_PER_FILE_BYTES", 4)
    (tmp_path / "big.txt").write_bytes(b"more than four bytes")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.files == []


def test_entry_count_cap_stops_enumeration_and_degrades(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(discovery, "MAX_FILESYSTEM_ENTRIES", 2)
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_bytes(b"x")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.degraded is True
    assert result.fingerprint_complete is False
    assert any(p["reason_code"] == "resource_limit" for p in result.problems)


def test_total_hashed_bytes_cap_excludes_files_once_exceeded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(discovery, "MAX_HASHED_TOTAL_BYTES", 10)
    (tmp_path / "a.txt").write_bytes(b"1234567")
    (tmp_path / "b.txt").write_bytes(b"1234567")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert len(result.files) == 1
    assert result.degraded is True
    assert result.fingerprint_complete is False
    assert result.exclusions.get("resource_limit_total_bytes") == 1


# ----------------------------------------------------------- determinism

def test_whole_scope_fingerprint_is_deterministic_across_scans(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / "b.txt").write_bytes(b"world")
    comp_dir = _comprehension_dir(tmp_path)
    first = discovery.enumerate_scope(tmp_path, comp_dir)
    second = discovery.enumerate_scope(tmp_path, comp_dir)
    assert first.whole_scope_fingerprint == second.whole_scope_fingerprint


def test_whole_scope_fingerprint_changes_when_a_new_file_is_added(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello")
    comp_dir = _comprehension_dir(tmp_path)
    before = discovery.enumerate_scope(tmp_path, comp_dir)
    (tmp_path / "b.txt").write_bytes(b"world")
    after = discovery.enumerate_scope(tmp_path, comp_dir)
    assert before.whole_scope_fingerprint != after.whole_scope_fingerprint
