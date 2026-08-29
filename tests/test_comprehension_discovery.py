"""#55 slice-1 PR-B item 2: file enumeration, platform/path policy, and the
three pre-freshness resource caps (DESIGN-55-comprehension-plane.md, "Scan
behavior" step 4 and "Resource caps").
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
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


def test_git_as_a_regular_file_is_hard_excluded_not_enumerated(tmp_path: Path) -> None:
    """M2 (sixth cold read, fix round 10): a git WORKTREE or submodule
    checkout stores `.git` as a REGULAR FILE (a `gitdir: ...` pointer),
    not a directory - previously neither excluded nor counted, so it was
    published as an addressable unit AND folded into the whole-scope
    fingerprint. That pointer names an absolute path that differs per
    worktree/machine even for the exact same commit, so two worktrees of
    the same commit could never fingerprint equal - the exact field
    PR-C freshness gates on."""
    (tmp_path / ".git").write_text(
        "gitdir: /some/absolute/path/that/differs/per/worktree\n", encoding="utf-8")
    (tmp_path / "a.txt").write_bytes(b"hello")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert [f.relative_path for f in result.files] == ["a.txt"]
    # >= 1, not == 1: the .agenttalk/ fixture directory itself is ALSO
    # hard_excluded in every run of this test (same convention as
    # test_enumerate_scope_excludes_default_directory_categories above).
    assert result.exclusions.get("hard_excluded", 0) >= 1


def test_two_worktrees_of_the_same_commit_fingerprint_equal(tmp_path: Path) -> None:
    """M2 (sixth cold read, fix round 10): the whole-scope fingerprint
    must depend only on real, shared content - never on a worktree's own
    `.git` pointer file, whose absolute-path content differs per
    worktree even for an otherwise byte-identical checkout."""
    worktree_a = tmp_path / "wt-a"
    worktree_b = tmp_path / "wt-b"
    for worktree, gitdir in ((worktree_a, "/repo/.git/worktrees/a"), (worktree_b, "/repo/.git/worktrees/b")):
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        (worktree / "a.txt").write_bytes(b"hello")
    comp_dir_a = _comprehension_dir(worktree_a)
    comp_dir_b = _comprehension_dir(worktree_b)
    result_a = discovery.enumerate_scope(worktree_a, comp_dir_a)
    result_b = discovery.enumerate_scope(worktree_b, comp_dir_b)
    assert result_a.whole_scope_fingerprint == result_b.whole_scope_fingerprint


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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory junctions only")
def test_enumerate_scope_records_a_windows_junction_as_a_boundary_and_never_follows_it(
    tmp_path: Path,
) -> None:
    """Cold-read B1 (reviewer, PR-B fix round 3): a directory JUNCTION is a
    reparse point but NOT a symlink proper - ``Path.is_symlink()`` returns
    False for one, so a boundary check keyed on that alone would descend
    into it, hash content living outside the project root, and fold it
    into the whole-scope fingerprint. Needs no special privilege on
    Windows (unlike a real symlink) - ``mklink /J`` works for any local
    user - so this executes unconditionally on every Windows leg, no skip
    possible."""
    outside = tmp_path.parent / "outside-discovery-junction-target"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_bytes(b"outside content")
    junction = tmp_path / "junction-to-outside"
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True, capture_output=True, text=True,
    )
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.files == []
    assert len(result.boundaries) == 1
    assert result.boundaries[0].relative_path == "junction-to-outside"
    assert result.boundaries[0].boundary_kind == "reparse_point"


def test_a_stat_failure_on_the_junction_check_is_treated_as_a_boundary_not_ordinary(
    tmp_path: Path, monkeypatch,
) -> None:
    """N3 (seventh cold read, fix round 11): a single lstat() call is the
    ONLY way a symlink or a Windows directory junction is told apart
    from an ordinary directory - failing it used to return None
    (ordinary, safe to enter) or (the first cut of this fix) crash
    outright via an uncaught OSError inside entry.is_symlink() itself.
    Must fail CLOSED: treated as a boundary (never entered) and a named
    problem, degrading the fingerprint. lstat() is called unconditionally
    regardless of platform, so this runs on every OS with no need to
    force a platform-specific code path - CI itself caught the first
    version of this test forcing os.name globally, which corrupted
    pathlib's own Path-class selection process-wide and crashed unrelated
    tests on non-Windows legs."""
    suspect = tmp_path / "unverifiable-entry"
    suspect.mkdir()
    (suspect / "inner.txt").write_bytes(b"should never be enumerated")

    real_lstat = Path.lstat

    def _lstat(self: Path, *args, **kwargs):
        if self.name == "unverifiable-entry":
            raise OSError("cannot stat")
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", _lstat)
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.files == []
    assert len(result.boundaries) == 1
    assert result.boundaries[0].relative_path == "unverifiable-entry"
    assert result.boundaries[0].boundary_kind == "unverifiable"
    assert any(p["reason_code"] == "parse_failed" and p["path"] == "unverifiable-entry"
               for p in result.problems)
    assert result.fingerprint_complete is False


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


def test_gitmodules_parsing_does_not_match_a_pathspec_key_as_path(tmp_path: Path) -> None:
    """N7 (fourth cold read, fix round 6): the old check was
    stripped.startswith("path") - a DIFFERENT git-config key that merely
    starts with the same letters (pathspec is a real git config key)
    would be silently treated as if it were a submodule's own path. The
    key must equal "path" exactly. A real directory sits at the
    "pathspec" line's own value, so the old bug would have skipped its
    contents entirely - this proves they are enumerated normally
    instead."""
    (tmp_path / ".gitmodules").write_text(
        '[submodule "lib"]\n'
        "\tpathspec = decoy\n"
        "\turl = https://example.invalid/lib.git\n",
        encoding="utf-8",
    )
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    (decoy_dir / "inner.txt").write_bytes(b"must be enumerated normally")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.boundaries == []
    assert sorted(f.relative_path for f in result.files) == [".gitmodules", "decoy/inner.txt"]


def test_unreadable_gitmodules_records_a_problem_and_marks_the_fingerprint_incomplete(
    tmp_path: Path, monkeypatch,
) -> None:
    """N2 (seventh cold read, fix round 11): an unreadable root
    .gitmodules used to silently return an EMPTY boundary set,
    indistinguishable from "no submodules at all" - a real submodule
    then walked straight into the fingerprint with
    fingerprint_complete: true. Fail-open against the choke-point
    discipline: an unreadable .gitmodules IS an enumeration omission
    (you cannot know what you failed to exclude) and must record a
    problem and mark the fingerprint incomplete, same as every other
    bounded-problem exit in this module."""
    (tmp_path / ".gitmodules").write_text(
        '[submodule "lib"]\n\tpath = lib\n\turl = https://example.invalid/lib.git\n',
        encoding="utf-8",
    )
    submodule_dir = tmp_path / "lib"
    submodule_dir.mkdir()
    (submodule_dir / "inner.txt").write_bytes(b"should never be enumerated")

    real_read_text = Path.read_text

    def _read_text(self: Path, *args, **kwargs):
        if self.name == ".gitmodules":
            raise OSError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert any(p["reason_code"] == "parse_failed" and p["path"] == ".gitmodules"
               for p in result.problems)
    assert result.fingerprint_complete is False
    assert result.whole_scope_fingerprint is None
    # the submodule directory is walked into (unknown, not excluded) since
    # its boundary could not be identified - the honest, visible failure
    # mode this fix trades for the old silent one.
    assert any(f.relative_path == "lib/inner.txt" for f in result.files)


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
    """N5 (cold-read, PR-B fix round 3): the previous version of this
    test asserted only `result.files == []`, which a read-then-discard
    implementation would ALSO satisfy just as well as one that genuinely
    never reads oversized content - the assertion could not tell the two
    apart, despite the test's own name and docstring claiming otherwise.
    Directly instruments Path.read_bytes to prove the oversized file is
    never read at all (with a sanity check that the tracking wrapper
    itself does observe the small file's real read, so a no-op wrapper
    could not silently pass this test either)."""
    monkeypatch.setattr(discovery, "MAX_PER_FILE_BYTES", 4)
    (tmp_path / "big.txt").write_bytes(b"more than four bytes")
    (tmp_path / "small.txt").write_bytes(b"ok")

    read_names: list[str] = []
    real_read_bytes = Path.read_bytes

    def _tracking_read_bytes(self: Path) -> bytes:
        read_names.append(self.name)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _tracking_read_bytes)

    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert [f.relative_path for f in result.files] == ["small.txt"]
    assert "big.txt" not in read_names, (
        "the oversized file's content was read despite the per-file size cap")
    assert "small.txt" in read_names, (
        "sanity check: the tracking wrapper must observe a real read, or this test "
        "would pass even with a no-op wrapper"
    )


def test_entry_count_cap_stops_enumeration_and_degrades(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(discovery, "MAX_FILESYSTEM_ENTRIES", 2)
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_bytes(b"x")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.degraded is True
    assert result.fingerprint_complete is False
    assert any(p["reason_code"] == "resource_limit" for p in result.problems)


def test_nesting_depth_cap_degrades_instead_of_crashing(tmp_path: Path, monkeypatch) -> None:
    """N6-nesting (cold-read, PR-B fix round 3): _walk recursed with no
    depth limit and only caught OSError - a pathologically deep directory
    tree could raise a bare RecursionError that propagated uncaught,
    crashing the whole scan rather than degrading it with a bounded
    problem."""
    monkeypatch.setattr(discovery, "MAX_NESTING_DEPTH", 3)
    current = tmp_path
    for i in range(6):
        current = current / f"d{i}"
        current.mkdir()
    (current / "deep.txt").write_bytes(b"x")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.files == []
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


# ----------------------- fingerprint completeness on error/exclusion exits (B-1, round 5)

def test_an_unlistable_directory_marks_the_fingerprint_incomplete(
    tmp_path: Path, monkeypatch,
) -> None:
    """B-1 (third cold read, fix round 5): only the four resource-cap exits
    used to clear ``fingerprint_complete``; the other four bounded-problem
    exits (unlistable directory, stat failure, unreadable bytes,
    unrepresentable filename) recorded a problem and left the flag True -
    a fingerprint computed over a provably incomplete file set published
    as complete, the exact fail-open the design forbids."""
    (tmp_path / "ok.txt").write_bytes(b"fine")
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "inside.txt").write_bytes(b"never seen")

    real_iterdir = Path.iterdir

    def _iterdir(self: Path):
        if self == blocked:
            raise OSError("permission denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _iterdir)
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert any(p["reason_code"] == "parse_failed" for p in result.problems)
    assert result.fingerprint_complete is False
    assert result.whole_scope_fingerprint is None


def test_an_unreadable_files_bytes_mark_the_fingerprint_incomplete(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / "ok.txt").write_bytes(b"fine")
    (tmp_path / "bad.txt").write_bytes(b"content")

    real_read_bytes = Path.read_bytes

    def _read_bytes(self: Path):
        if self.name == "bad.txt":
            raise OSError("cannot read")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _read_bytes)
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert any(p["reason_code"] == "parse_failed" for p in result.problems)
    assert result.fingerprint_complete is False
    assert result.whole_scope_fingerprint is None


def test_an_unreadable_files_problem_detail_never_embeds_the_absolute_path(
    tmp_path: Path, monkeypatch,
) -> None:
    """M-3 (third cold read, fix round 5): str(exc) on a REAL OSError
    embeds the exception's own absolute filename (unlike a bare
    OSError("message"), which does not - a test using one would not
    reproduce the leak at all). ``detail`` must carry only a fixed,
    named template and the OS's own short strerror, never that
    filename. Checked via a plain alphanumeric marker (tmp_path's own
    leaf name), not the raw path string: on Windows, str(exc)'s own
    formatting already backslash-escapes the embedded filename, so a
    literal ``str(tmp_path) in detail`` check would never match the
    escaped form either way - the marker has no such special characters
    and survives that escaping unchanged."""
    (tmp_path / "bad.txt").write_bytes(b"content")
    root_marker = tmp_path.resolve().name

    def _read_bytes(self: Path):
        if self.name == "bad.txt":
            raise OSError(13, "Permission denied", str(self))
        raise AssertionError("unexpected read of a different file")

    monkeypatch.setattr(Path, "read_bytes", _read_bytes)
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    problem = next(p for p in result.problems if p["path"] == "bad.txt")
    assert root_marker not in problem["detail"]
    assert "Permission denied" in problem["detail"]


def test_an_unrepresentable_filename_marks_the_fingerprint_incomplete(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / "ok.txt").write_bytes(b"fine")
    comp_dir = _comprehension_dir(tmp_path)
    monkeypatch.setattr(
        discovery, "_non_utf8_path_problem_detail",
        lambda relative: (
            {"path": relative, "detail": "simulated non-utf8 path"}
            if relative == "ok.txt" else None
        ),
    )
    (tmp_path / "ordinary.txt").write_bytes(b"also fine")
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert any(p["reason_code"] == "non_utf8_path" for p in result.problems)
    assert result.fingerprint_complete is False
    assert result.whole_scope_fingerprint is None


# ----------------------------------------------------------- non-UTF-8 paths (note 5)

def test_non_utf8_path_problem_detail_recognizes_a_lone_surrogate():
    """Note 5 (second cold read, fix round 4): on POSIX, a filename with
    bytes that are not valid UTF-8 decodes (via surrogateescape) to a
    string containing lone surrogates - encode("utf-8") on that string
    raises UnicodeEncodeError, which previously surfaced as an unhandled
    traceback at artifact-write time rather than a typed, bounded problem.
    Tested at the function level (not via a real non-UTF-8 filename on
    disk, which is POSIX-only and not reliably constructible from every
    dev/CI platform) - _non_utf8_path_problem_detail is factored out of
    the enumeration walk specifically to make this possible."""
    surrogate_laden = "weird-\udcff-name.txt"
    with pytest.raises(UnicodeEncodeError):
        surrogate_laden.encode("utf-8")  # sanity: this IS the failure mode

    result = discovery._non_utf8_path_problem_detail(surrogate_laden)
    assert result is not None
    assert result["path"].encode("ascii")  # the returned path is ASCII-safe, unlike the input
    assert "not valid UTF-8" in result["detail"]


def test_non_utf8_path_problem_detail_is_none_for_an_ordinary_path():
    assert discovery._non_utf8_path_problem_detail("p/Foo.java") is None


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


# ------------------------- effective exclude-rule digest (N2, round 6)

def test_effective_exclude_rule_digest_is_deterministic() -> None:
    """N2 (fourth cold read, fix round 6): scan.json now publishes a
    digest over the CURRENT hardcoded exclude-rule sets, so a future
    change to any of them is independently detectable even before
    config.json exists to make them caller-configurable."""
    assert discovery.effective_exclude_rule_digest() == discovery.effective_exclude_rule_digest()


def test_effective_exclude_rule_digest_changes_when_a_rule_set_changes(monkeypatch) -> None:
    before = discovery.effective_exclude_rule_digest()
    monkeypatch.setattr(
        discovery, "_GENERATED_VENDOR_DIR_NAMES",
        frozenset({*discovery._GENERATED_VENDOR_DIR_NAMES, "a-new-vendor-dir"}),
    )
    after = discovery.effective_exclude_rule_digest()
    assert before != after
