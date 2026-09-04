"""#55 slice-1 PR-B item 2: file enumeration, platform/path policy, and the
three pre-freshness resource caps (DESIGN-55-comprehension-plane.md, "Scan
behavior" step 4 and "Resource caps").
"""

from __future__ import annotations

import hashlib
import os
import shutil
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


# ------------------------------------------ FIX ROUND 18 F1: any-depth source roots


@pytest.mark.parametrize("source_root_dir,marker_name", [
    # a multi-module Maven/Gradle reactor - the source root is one
    # directory DEEPER than repo root, not repo root itself.
    ("core/src/main/java", "out"),
    # Kotlin/Groovy/Scala source roots, same standard-layout convention,
    # different language directory name.
    ("src/main/kotlin", "out"),
    ("src/main/groovy", "out"),
    ("src/main/scala", "out"),
    # a webapp tree.
    ("src/main/webapp", "build"),
])
def test_enumerate_scope_recognizes_a_source_root_at_any_depth(
    tmp_path: Path, source_root_dir: str, marker_name: str,
) -> None:
    """FIX ROUND 18 (fourteenth cold read, F1 BLOCKER, wrong-data): mirrors
    the reader's own ``.cr14-hex``/``.cr14-hex2`` shapes (source only -
    their published RUN artifact is deliberately tampered, never treated
    as ground truth here). The round-16 B2 source-root carve-out was
    repo-root-anchored and missed every one of these standard layouts -
    a generated/vendor-NAMED directory nested under a source root that
    is not itself repo root must still be recognized as in-scope, not
    silently excluded."""
    marker_dir = tmp_path / source_root_dir / "com" / "acme" / "port" / marker_name
    marker_dir.mkdir(parents=True)
    (marker_dir / "Inner.java").write_bytes(b"class Inner {}\n")
    comp_dir = _comprehension_dir(tmp_path)

    result = discovery.enumerate_scope(tmp_path, comp_dir)

    assert any(f.relative_path.endswith("Inner.java") for f in result.files)
    assert not any(
        e["category"] == "generated_or_vendor" and e["path"].endswith(marker_name)
        for e in result.excluded_roots
    )


def test_enumerate_scope_still_excludes_a_real_module_root_build_directory(
    tmp_path: Path,
) -> None:
    """FIX ROUND 18 (fourteenth cold read, F1 BLOCKER, wrong-data): the
    any-depth source-root pattern match must NOT become "anything nested
    inside a module is exempt" - a module-root build-output directory
    with no ``src/main/...``-style segment anywhere in its own path
    (``core/build/``) is a genuine build artifact and must stay
    excluded, even though ``core/`` also happens to contain a real
    source root elsewhere in the same module."""
    (tmp_path / "core" / "src" / "main" / "java").mkdir(parents=True)
    build_dir = tmp_path / "core" / "build"
    build_dir.mkdir()
    (build_dir / "Generated.class").write_bytes(b"\x00\x01binary")
    comp_dir = _comprehension_dir(tmp_path)

    result = discovery.enumerate_scope(tmp_path, comp_dir)

    assert not any(f.relative_path.endswith("Generated.class") for f in result.files)
    assert any(
        e["category"] == "generated_or_vendor" and e["path"] == "core/build"
        for e in result.excluded_roots
    )


def test_enumerate_scope_does_not_recognize_a_bare_ant_style_java_root(
    tmp_path: Path,
) -> None:
    """FIX ROUND 18 (fourteenth cold read, F1 BLOCKER, JUDGE): a bare
    Ant-style ``java/`` root (no ``src/main``/``src/test`` scaffolding)
    is deliberately NOT recognized - see the named-limit comment beside
    ``_RECOGNIZED_SOURCE_ROOT_SEGMENT_RE``. A generated/vendor-named
    directory nested under it stays excluded, same as before this
    round."""
    marker_dir = tmp_path / "java" / "com" / "acme" / "port" / "out"
    marker_dir.mkdir(parents=True)
    (marker_dir / "Inner.java").write_bytes(b"class Inner {}\n")
    comp_dir = _comprehension_dir(tmp_path)

    result = discovery.enumerate_scope(tmp_path, comp_dir)

    assert not any(f.relative_path.endswith("Inner.java") for f in result.files)
    assert any(
        e["category"] == "generated_or_vendor" and e["path"].endswith("java/com/acme/port/out")
        for e in result.excluded_roots
    )


def test_enumerate_scope_degrades_an_ant_style_vendor_dir_that_contains_real_code(
    tmp_path: Path,
) -> None:
    """FIX ROUND 19 (fifteenth cold read, F4 MAJOR, wrong-data): a bare
    ``src/`` root (Ant/Eclipse convention, never recognized as a source
    root - no ``main``/``test`` scaffolding) has a DOMAIN package
    literally named ``vendor`` excluded as ``generated_or_vendor`` on a
    run that would otherwise report complete - factually wrong, since
    the directory holds real, hand-written ``.java`` source. Must
    degrade the run with a named problem, the same standard round 18's
    own F6 already established for a single binary-sniffed file."""
    vendor_dir = tmp_path / "src" / "vendor"
    vendor_dir.mkdir(parents=True)
    (vendor_dir / "Helper.java").write_text("package vendor;\nclass Helper {}\n", encoding="utf-8")
    comp_dir = _comprehension_dir(tmp_path)

    result = discovery.enumerate_scope(tmp_path, comp_dir)

    assert not any(f.relative_path.endswith("Helper.java") for f in result.files)
    assert any(
        e["category"] == "generated_or_vendor" and e["path"] == "src/vendor"
        for e in result.excluded_roots
    )
    assert result.degraded is True
    assert any(p["reason_code"] == "excluded_region_contains_code" for p in result.problems)


def test_enumerate_scope_a_real_repo_root_build_output_dir_stays_silent(
    tmp_path: Path,
) -> None:
    """Companion negative case - a genuine build-output directory at
    repo root (no ``src`` segment anywhere in its own path at all) must
    stay silent even when it happens to contain a code-bearing
    extension - a target/ full of annotation-processor-GENERATED
    .java is the classic, ordinary Maven case; a blanket degrading rule
    would re-degrade every normal Maven repo, the exact regression
    round 16b's own B4 calibration already fixed once for tier 2.

    FIX ROUND 21 (seventeenth cold read, CR17-5 MAJOR, completeness -
    calibration): this fixture's own ``target/generated-sources/`` is
    now the EXACT recognized generated-output position CR17-5 exempts -
    the poison rule no longer fires either, not just F4's own
    degradation boundary. Before this round, EVERY compiled Maven repo
    (MapStruct/Lombok/JPA-metamodel/protobuf-generated .java is
    ubiquitous under this exact path) poisoned its own entire
    externality surface - the single most common repo state. See the
    companion test below for the position that still poisons (a
    code-bearing file sitting anywhere ELSE inside the same excluded
    root)."""
    target_dir = tmp_path / "target" / "generated-sources"
    target_dir.mkdir(parents=True)
    (target_dir / "Generated.java").write_text("package p;\nclass Generated {}\n", encoding="utf-8")
    comp_dir = _comprehension_dir(tmp_path)

    result = discovery.enumerate_scope(tmp_path, comp_dir)

    assert not any(f.relative_path.endswith("Generated.java") for f in result.files)
    assert any(
        e["category"] == "generated_or_vendor" and e["path"] == "target"
        for e in result.excluded_roots
    )
    assert result.degraded is False
    assert not any(p["reason_code"] == "excluded_region_contains_code" for p in result.problems)
    assert result.excluded_region_may_contain_target is False
    assert result.poisoning_excluded_roots == []


def test_enumerate_scope_a_built_checkouts_target_classes_stays_silent(
    tmp_path: Path,
) -> None:
    """MICRO-ROUND 49 (forty-third cold read, C1, wrong-data): unlike
    ``target/generated-sources/`` above (build-tool-GENERATED source),
    ``target/classes/`` holds resources COPIED byte-identical from
    ``src/main/resources`` by the build's own resource-processing step -
    a `.sql`/`.jsp` there has no code-generation angle at all, but was
    NOT a recognized position before this round, so the single most
    ordinary repo state (anyone who ran a build before committing)
    poisoned this producer's entire externality surface for a shape
    with zero suspicious content."""
    classes_dir = tmp_path / "target" / "classes"
    classes_dir.mkdir(parents=True)
    (classes_dir / "schema.sql").write_text("CREATE TABLE t (id INT);\n", encoding="utf-8")
    comp_dir = _comprehension_dir(tmp_path)

    result = discovery.enumerate_scope(tmp_path, comp_dir)

    assert not any(f.relative_path.endswith("schema.sql") for f in result.files)
    assert any(
        e["category"] == "generated_or_vendor" and e["path"] == "target"
        for e in result.excluded_roots
    )
    assert result.degraded is False
    assert not any(p["reason_code"] == "excluded_region_contains_code" for p in result.problems)
    assert result.excluded_region_may_contain_target is False
    assert result.poisoning_excluded_roots == []


def test_enumerate_scope_a_first_party_java_only_under_target_classes_still_poisons(
    tmp_path: Path,
) -> None:
    """MICRO-ROUND 49b (BLOCKER, reviewer-3's own two-SHA repro - a
    regression the C1 fix above introduced): the first version of the
    C1 fix exempted target/classes/ the SAME way as generated-sources/
    (any code-bearing extension, .java included) - but a build never
    legitimately COPIES .java into classes/ (only compiles it away
    entirely). A real, first-party class existing ONLY under target/
    classes/ (no src/ copy at all) is exactly the vendored/stray-real-
    code shape the poison rule exists to catch, and the first version
    wrongly exempted it too. Reproduced pre-fix exactly as reported:
    poisoning_excluded_roots was empty, must be non-empty again."""
    classes_dir = tmp_path / "target" / "classes" / "gen"
    classes_dir.mkdir(parents=True)
    (classes_dir / "Mapper.java").write_text(
        "package gen;\nclass Mapper {}\n", encoding="utf-8")
    comp_dir = _comprehension_dir(tmp_path)

    result = discovery.enumerate_scope(tmp_path, comp_dir)

    assert not any(f.relative_path.endswith("Mapper.java") for f in result.files)
    assert result.excluded_region_may_contain_target is True
    assert any(r["path"] == "target" for r in result.poisoning_excluded_roots)


def test_enumerate_scope_a_code_bearing_file_outside_the_generated_position_still_poisons(
    tmp_path: Path,
) -> None:
    """FIX ROUND 21 (CR17-5 MAJOR): the exemption is by POSITION, never
    by the excluded root's own name or the root as a whole - a .java
    sitting directly under ``target/`` (NOT inside ``generated-
    sources/``/``generated-test-sources/``) is exactly the vendored-
    module/stray-src-build shape the poison rule exists to catch, and
    must still poison, unchanged."""
    target_dir = tmp_path / "target" / "some-vendor-module"
    target_dir.mkdir(parents=True)
    (target_dir / "Vendored.java").write_text(
        "package p;\nclass Vendored {}\n", encoding="utf-8")
    comp_dir = _comprehension_dir(tmp_path)

    result = discovery.enumerate_scope(tmp_path, comp_dir)

    assert result.excluded_region_may_contain_target is True
    assert any(r["path"] == "target" for r in result.poisoning_excluded_roots)


def test_enumerate_scope_a_vendor_dir_with_real_code_poisons_without_degrading(
    tmp_path: Path,
) -> None:
    """FIX ROUND 20 (sixteenth cold read, M1+M2 MAJOR, wrong-data - the
    POISON RULE): a generated/vendor-named directory at repo root
    (no src ancestor at all - never F4's own degradation boundary)
    that genuinely contains hand-written code (the mainstream Maven
    vendored-module shape, ``vendor/<module>/src/main/java/...``, is
    exactly this - a plain ``vendor`` name at repo root) must still
    poison confident externality run-wide, even though it never
    degrades the run on its own (m2's own JUDGE: declare the boundary
    honestly via the poison rule + projection, rather than widening F4's
    own degradation)."""
    vendor_dir = tmp_path / "vendor" / "some-module" / "src" / "main" / "java" / "com" / "acme"
    vendor_dir.mkdir(parents=True)
    (vendor_dir / "Widget.java").write_text("package com.acme;\nclass Widget {}\n", encoding="utf-8")
    comp_dir = _comprehension_dir(tmp_path)

    result = discovery.enumerate_scope(tmp_path, comp_dir)

    assert not any(f.relative_path.endswith("Widget.java") for f in result.files)
    assert any(
        e["category"] == "generated_or_vendor" and e["path"] == "vendor"
        for e in result.excluded_roots
    )
    assert result.degraded is False
    assert result.excluded_region_may_contain_target is True


def test_a_poison_status_flip_inside_an_excluded_region_changes_the_fingerprint(
    tmp_path: Path,
) -> None:
    """MICRO-ROUND 49 (forty-third cold read, C2, judged): a file added
    entirely inside an already-excluded vendor region does not, by
    itself, change whole_scope_fingerprint (FINGERPRINT_CAVEAT's own
    accepted residual) - but if that file flips whether the region
    POISONS externality resolution, real published dependency-
    resolution facts change while the fingerprint used to report no
    change at all. Two scans of the identical directory, differing
    ONLY in whether the vendor dir's own file is code-bearing, must now
    produce DIFFERENT fingerprints."""
    root_a = tmp_path / "a"
    (root_a / "vendor" / "some-module").mkdir(parents=True)
    (root_a / "vendor" / "some-module" / "README.txt").write_text("notes\n", encoding="utf-8")
    result_a = discovery.enumerate_scope(root_a, _comprehension_dir(root_a))
    assert result_a.poisoning_excluded_roots == []

    root_b = tmp_path / "b"
    (root_b / "vendor" / "some-module").mkdir(parents=True)
    (root_b / "vendor" / "some-module" / "Widget.java").write_text(
        "package com.acme;\nclass Widget {}\n", encoding="utf-8")
    result_b = discovery.enumerate_scope(root_b, _comprehension_dir(root_b))
    assert any(r["path"] == "vendor" for r in result_b.poisoning_excluded_roots)

    assert result_a.whole_scope_fingerprint != result_b.whole_scope_fingerprint


def test_enumerate_scope_an_ant_style_build_dir_with_only_binaries_stays_silent(
    tmp_path: Path,
) -> None:
    """Companion negative case - a bare src/ root's own ``build``
    directory containing only compiled ``.class`` output (no adapter-
    handled or tier-2 extension at all) stays silent - the boundary
    rule requires BOTH conditions (an uncarved src/ ancestor AND a real
    code-bearing extension inside), never just one."""
    build_dir = tmp_path / "src" / "build"
    build_dir.mkdir(parents=True)
    (build_dir / "Helper.class").write_bytes(b"\xca\xfe\xba\xbe")
    comp_dir = _comprehension_dir(tmp_path)

    result = discovery.enumerate_scope(tmp_path, comp_dir)

    assert any(
        e["category"] == "generated_or_vendor" and e["path"] == "src/build"
        for e in result.excluded_roots
    )
    assert result.degraded is False
    assert result.excluded_region_may_contain_target is False
    assert not any(p["reason_code"] == "excluded_region_contains_code" for p in result.problems)


class _FakeExcludedDirEntry:
    """Mimics the subset of ``os.DirEntry`` the peek function uses -
    lets a test control PEEK-ORDER deterministically without creating
    thousands of real files, and without depending on any real
    filesystem's own (unspecified) directory-listing order."""

    def __init__(
        self, directory: Path, name: str, *, is_dir: bool = False, is_symlink: bool = False,
    ) -> None:
        self.name = name
        self.path = str(directory / name)
        self._is_dir = is_dir
        self._is_symlink = is_symlink

    def is_symlink(self) -> bool:
        return self._is_symlink

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return self._is_dir

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        return not self._is_dir


def test_enumerate_scope_reports_the_same_degraded_outcome_regardless_of_peek_order(
    tmp_path: Path, monkeypatch,
) -> None:
    """FIX ROUND 19b (reviewer-3's rejection of round 19, THE MAJOR,
    wrong-data): the peek's own entry-count cap used to fold BOTH
    "fully explored, genuinely no code" AND "exceeded the cap before
    finding out" into the identical bare ``False`` - the caller then
    read cap-exhaustion as a confident negative. Measured: two repos
    differing ONLY in the FILENAME of the one .java among thousands of
    .class files inside a bare src/build/ (the normal Ant-era shape,
    easily past the cap) published DIFFERENT run status purely because
    scan order happened to visit one filename before the cap and the
    other after it - a published outcome depending on filesystem
    enumeration order. Both orderings must now degrade - one via
    excluded_region_contains_code (found within the cap), the other via
    excluded_region_peek_truncated (the cap exhausted first, an
    honestly UNKNOWN outcome, never silently treated as absent)."""
    build_dir = tmp_path / "src" / "build"
    build_dir.mkdir(parents=True)
    bulk = [
        _FakeExcludedDirEntry(build_dir, f"Compiled{i}.class")
        for i in range(discovery._MAX_EXCLUDED_DIRECTORY_PEEK_ENTRIES + 10)
    ]
    real_scandir = discovery.os.scandir

    def _run_with_java_at(position: int):
        entries = list(bulk)
        entries.insert(position, _FakeExcludedDirEntry(build_dir, "Real.java"))

        def _fake_scandir(path):
            # FIX ROUND 20 (CI-round fix - discovered while investigating a
            # cross-platform dev-gate failure on this PR's own tip):
            # `discovery.os` IS the process-wide `os` module - patching
            # `scandir` here intercepts EVERY caller, not just discovery.py's
            # own. On POSIX (never Windows - the exact reason this passed on
            # every windows leg and failed on every linux/macos one),
            # `shutil.rmtree` internally calls `os.scandir(topfd)` with a raw
            # integer file descriptor while cleaning up an unrelated
            # directory (pytest's own tmp_path teardown) - `Path(path)`
            # unconditionally wrapped an int and raised TypeError instead of
            # falling through to the real scandir.
            if isinstance(path, (str, Path)) and Path(path) == build_dir:
                return entries
            return real_scandir(path)

        monkeypatch.setattr(discovery.os, "scandir", _fake_scandir)
        comp_dir = _comprehension_dir(tmp_path)
        return discovery.enumerate_scope(tmp_path, comp_dir)

    early_result = _run_with_java_at(0)
    late_result = _run_with_java_at(len(bulk))

    assert early_result.degraded is True
    assert any(
        p["reason_code"] == "excluded_region_contains_code" for p in early_result.problems)
    assert late_result.degraded is True
    assert any(
        p["reason_code"] == "excluded_region_peek_truncated" for p in late_result.problems)


def test_enumerate_scope_a_fully_explored_excluded_dir_under_the_cap_stays_silent(
    tmp_path: Path,
) -> None:
    """Companion negative case - a bare src/ root's own excluded
    directory, fully explored WELL under the peek cap and genuinely
    free of any code-bearing extension, must stay silent (not
    truncated, not degraded) - the truncation problem is for exceeding
    the cap specifically, never merely for existing."""
    build_dir = tmp_path / "src" / "build"
    build_dir.mkdir(parents=True)
    (build_dir / "Helper.class").write_bytes(b"\xca\xfe\xba\xbe")
    comp_dir = _comprehension_dir(tmp_path)

    result = discovery.enumerate_scope(tmp_path, comp_dir)

    assert result.degraded is False
    assert not any(
        p["reason_code"] == "excluded_region_peek_truncated" for p in result.problems)
    assert not any(
        p["reason_code"] == "excluded_region_contains_code" for p in result.problems)


def test_enumerate_scope_a_symlink_inside_an_excluded_dir_degrades_as_truncated_not_silent(
    tmp_path: Path, monkeypatch,
) -> None:
    """FIX ROUND 20 (sixteenth cold read, P6 MINOR, taken): the peek used
    to silently skip a symlinked subdirectory as if it were simply
    absent - folding "we deliberately never looked here" into the same
    confident ``False`` a genuinely explored, code-free directory
    returns. We do not know what the symlink points to (and never
    follow it - that part is unchanged, still the safe choice); this
    must now degrade the same honest, uncertain way cap-exhaustion
    already does (``excluded_region_peek_truncated``), never stay
    silent."""
    build_dir = tmp_path / "src" / "build"
    build_dir.mkdir(parents=True)
    real_scandir = discovery.os.scandir

    def _fake_scandir(path):
        # Same POSIX-only `shutil.rmtree(topfd=<int>)` leak documented on
        # the sibling fixture above - never wrap a raw fd in `Path()`.
        if isinstance(path, (str, Path)) and Path(path) == build_dir:
            return [_FakeExcludedDirEntry(build_dir, "link-to-somewhere", is_symlink=True)]
        return real_scandir(path)

    monkeypatch.setattr(discovery.os, "scandir", _fake_scandir)
    comp_dir = _comprehension_dir(tmp_path)

    result = discovery.enumerate_scope(tmp_path, comp_dir)

    assert result.degraded is True
    assert any(
        p["reason_code"] == "excluded_region_peek_truncated" for p in result.problems)


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


@pytest.mark.parametrize("filename", [
    ".netrc", "app.env", "production.env", "app.jks", "credentials",
    "secrets.properties", "service-account.key",
])
def test_enumerate_scope_excludes_round_32_widened_secret_file_patterns(
    tmp_path: Path, filename: str,
) -> None:
    """FIX ROUND 32 (twenty-eighth cold read, F5 MAJOR, completeness): the
    reader's own seven measured shapes - each used to leak its path and
    content digest as an ordinary discovered file, matching none of the
    OLD closed pattern set."""
    (tmp_path / filename).write_bytes(b"secret")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.files == []
    assert result.exclusions.get("secret") == 1


@pytest.mark.parametrize("filename", [
    ".pgpass", ".git-credentials", ".dockercfg", ".htpasswd", ".npmrc",
    "secrets.yaml", "application-secret.properties",
])
def test_enumerate_scope_excludes_round_35_widened_secret_file_patterns(
    tmp_path: Path, filename: str,
) -> None:
    """FIX ROUND 35 (twenty-ninth cold read, F4 MINOR, completeness): a
    further measured battery of seven canonical credential files - each
    used to leak its path and content digest as an ordinary discovered
    file, matching none of the pattern set as of round 32."""
    (tmp_path / filename).write_bytes(b"secret")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.files == []
    assert result.exclusions.get("secret") == 1


def test_enumerate_scope_excludes_a_secret_shaped_directory_name_not_just_files(
    tmp_path: Path,
) -> None:
    """M (cold-read PR-B fix round 47 completeness): secret-pattern
    matching used to apply to FILES only - a directory literally named
    ``.env`` (a real convention: a whole directory of per-environment
    secret files) walked its children uninhibited, publishing whatever
    was inside as ordinary discovered files. The SAME closed pattern set
    now also governs directory names, excluding the whole subtree the
    same safe way a secret-shaped file already is."""
    env_dir = tmp_path / ".env"
    env_dir.mkdir()
    (env_dir / "production").write_bytes(b"DB_PASSWORD=hunter2\n")
    (tmp_path / "App.java").write_bytes(b"class App {}\n")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert [f.relative_path for f in result.files] == ["App.java"]
    assert result.exclusions.get("secret") == 1


def test_exclusion_category_exempts_a_secret_shaped_directory_inside_a_recognized_source_root(
    tmp_path: Path,
) -> None:
    """FIX ROUND 48 (forty-second cold read, F1 BLOCKER leg 1, wrong-data
    - the round-16 pattern): a directory literally named ``credentials``
    (one of ``_SECRET_FILE_PATTERNS``'s own exact-literal entries) is
    ALSO a plausible ordinary Java package segment (``com/ex/
    credentials``) - one sitting inside an established ``src/main/
    java/...`` tree is real, hand-written source, not a credentials
    store, the same "domain package coincidentally named like the
    exclusion category" shape round 16 already fixed once for
    generated/vendor directory names. Without the source-root guard,
    this package silently vanished from the inventory with no code-
    bearing/poison signal at all (see the poison-gate test below)."""
    pkg_dir = tmp_path / "src" / "main" / "java" / "com" / "ex" / "credentials"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "Handler.java").write_text(
        "package com.ex.credentials;\nclass Handler {}\n", encoding="utf-8")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert "src/main/java/com/ex/credentials/Handler.java" in {
        f.relative_path for f in result.files}
    assert result.exclusions.get("secret", 0) == 0
    # MICRO-ROUND 49 (forty-third cold read, C3, completeness - the
    # visibility half): admitted via the exemption, but never silently -
    # a per-run problem records exactly this fact.
    matching = [
        p for p in result.problems
        if p["reason_code"] == "secret_shaped_path_admitted_via_source_root_exemption"]
    assert len(matching) == 1
    assert matching[0]["path"] == "src/main/java/com/ex/credentials"


def test_dot_prefixed_secret_literal_stays_excluded_even_inside_a_recognized_source_root(
    tmp_path: Path,
) -> None:
    """MICRO-ROUND 49 (forty-third cold read, C3, wrong-data): the round-
    48 source-root exemption above is OVER-WIDE for the seven DOT-
    PREFIXED secret literals - unlike ``credentials``, a dot-prefixed
    name (``.env``) can never be a legal Java package segment, so the
    "coincidental domain package" reasoning the exemption exists for
    never applies to one. A ``.env`` directory sitting under a
    recognized source root (``src/main/resources/``) is exactly as much
    a secrets store as one at the repo root - excluded regardless,
    reproduced pre-fix exactly as measured (published as a unit)."""
    env_dir = tmp_path / "src" / "main" / "resources" / ".env"
    env_dir.mkdir(parents=True)
    (env_dir / "production").write_bytes(b"DB_PASSWORD=hunter2\n")
    (tmp_path / "src" / "main" / "java" / "p").mkdir(parents=True)
    (tmp_path / "src" / "main" / "java" / "p" / "App.java").write_bytes(b"package p;\nclass App {}\n")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert "src/main/resources/.env/production" not in {f.relative_path for f in result.files}
    assert result.exclusions.get("secret", 0) == 1
    assert not any(
        p["reason_code"] == "secret_shaped_path_admitted_via_source_root_exemption"
        for p in result.problems)


def test_enumerate_scope_a_secret_shaped_directory_hiding_code_poisons_externality(
    tmp_path: Path,
) -> None:
    """FIX ROUND 48 (forty-second cold read, F1 BLOCKER legs 2+3,
    wrong-data, .cr42-secretdir): a directory named from the secret set,
    NOT inside a recognized source root (so genuinely excluded as
    "secret"), hiding real code one level deeper - round 20's own poison
    gate used to be `if category == "generated_or_vendor":`, blind to
    the "secret" category round 47 introduced, so this excluded
    directory's own code-bearing content never poisoned this run's
    externality confidence at all. Inverted to a category PROPERTY
    (`_DIRECTORY_CATEGORIES_THAT_CANNOT_HIDE_FIRST_PARTY_CODE`) so
    "secret" (and any future widened category) is poison-eligible by
    default."""
    credentials_dir = tmp_path / "credentials"
    nested = credentials_dir / "src" / "main" / "java" / "com" / "ex"
    nested.mkdir(parents=True)
    (nested / "Foo.java").write_text("package com.ex;\nclass Foo {}\n", encoding="utf-8")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.exclusions.get("secret") == 1
    assert result.excluded_region_may_contain_target is True
    assert any(p["path"] == "credentials" for p in result.poisoning_excluded_roots)
    # credentials/ itself sits under no bare `src/` segment (the src
    # scaffolding is nested DEEPER, invisible once pruned at this
    # boundary) - poisons externality but does not itself degrade,
    # parity with an equivalent generated/vendor exclusion at this
    # exact same position.
    assert not any(p["reason_code"] == "excluded_region_contains_code" for p in result.problems)


def test_enumerate_scope_a_secret_shaped_directory_hiding_code_under_a_bare_src_root_degrades(
    tmp_path: Path,
) -> None:
    """FIX ROUND 48 (F1 BLOCKER legs 2+3, parity with the existing
    generated/vendor degrade path): the SAME excluded-directory shape
    as above, but positioned so its OWN relative path sits under a bare,
    uncarved `src/` segment - the identical Ant-legacy position
    `excluded_region_contains_code` already degrades for a generated/
    vendor exclusion. Proves the widened poison gate did not just add
    the poison flag but also inherited the existing degrade trigger for
    the "secret" category, unchanged."""
    secret_dir = tmp_path / "app" / "src" / "credentials"
    secret_dir.mkdir(parents=True)
    (secret_dir / "Foo.java").write_text("package com.ex;\nclass Foo {}\n", encoding="utf-8")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.exclusions.get("secret") == 1
    assert result.fingerprint_complete is False
    matching = [p for p in result.problems if p["reason_code"] == "excluded_region_contains_code"]
    assert len(matching) == 1
    assert "'secret'-category" in matching[0]["detail"]


def test_enumerate_scope_an_empty_secret_shaped_directory_does_not_poison(tmp_path: Path) -> None:
    """Control: the same excluded-as-secret directory position as the
    poisoning test above, but genuinely empty (no code-bearing content)
    - excluded, but never poisons and never degrades, proving the fix
    does not overreact to every secret-shaped directory exclusion."""
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir()
    (credentials_dir / "readme.txt").write_text("nothing to see here\n", encoding="utf-8")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.exclusions.get("secret") == 1
    assert result.excluded_region_may_contain_target is False
    assert result.poisoning_excluded_roots == []
    assert result.problems == []


@pytest.mark.parametrize("dirname", ["venv", "node_modules", "__pycache__"])
def test_enumerate_scope_a_dependency_cache_shaped_directory_inside_a_recognized_source_root_is_inventoried(
    tmp_path: Path, dirname: str,
) -> None:
    """MICRO-ROUND 48b (reviewer-3's own attack on round 48's F1,
    corrected): unlike `vcs`/`hard_excluded` (both dot-prefixed - never
    a legal Java identifier), several `dependency_cache` directory
    names (`node_modules`, `venv`, `__pycache__`) ARE legal Java
    identifiers, so a real domain package could coincidentally share
    one - the same "domain package coincidentally named like the
    exclusion category" shape round 16 already fixed for generated/
    vendor names and round 48's own F1 already fixed for secret names,
    now applied to dependency_cache too via the identical source-root
    guard (never poison-eligibility - see the module-level comment
    above `_DIRECTORY_CATEGORIES_THAT_CANNOT_HIDE_FIRST_PARTY_CODE`)."""
    pkg_dir = tmp_path / "src" / "main" / "java" / "com" / "ex" / dirname
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "Handler.java").write_text(
        f"package com.ex.{dirname};\nclass Handler {{}}\n", encoding="utf-8")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert f"src/main/java/com/ex/{dirname}/Handler.java" in {
        f.relative_path for f in result.files}
    assert result.exclusions.get("dependency_cache", 0) == 0


def test_enumerate_scope_excludes_a_credentials_json_service_account_key(tmp_path: Path) -> None:
    """FIX ROUND 41 (thirty-fifth cold read, F7 POLISH, completeness): the
    bare ``credentials`` literal (an AWS-CLI-style file) was already
    closed, but the equally common ``credentials.json`` shape (a
    downloaded Google Cloud service-account key or OAuth client-secret
    file) was not - the same class of gap round 32/35 each closed for a
    different basename."""
    (tmp_path / "credentials.json").write_bytes(b'{"type": "service_account"}')
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.files == []
    assert result.exclusions.get("secret") == 1


def test_enumerate_scope_does_not_over_exclude_an_unrelated_credentials_source_file(
    tmp_path: Path,
) -> None:
    """FIX ROUND 41 (F7 POLISH, control): ``credentials.json`` is an EXACT
    literal, never a ``credentials.*`` glob - a real, unrelated source
    file that merely shares the "credentials" stem (e.g. a Go package's
    own ``credentials.go``) must not be swept up the way a wider glob
    would (the identical round-37 lesson ``secrets.*`` already learned
    for ``Secrets.java``)."""
    (tmp_path / "credentials.go").write_bytes(b"package aws\n")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert {f.relative_path for f in result.files} == {"credentials.go"}
    assert result.exclusions.get("secret", 0) == 0


def test_enumerate_scope_does_not_over_exclude_a_secrets_glob_false_positive(
    tmp_path: Path,
) -> None:
    """FIX ROUND 35 (F4's own weighed disposition): the new ``secrets.*``
    glob is deliberately narrower than a bare ``*secret*`` - it matches
    only a basename that STARTS WITH the literal "secrets." (so
    "secrets.yaml" is excluded), never a hyphenated or otherwise
    differently-shaped name that merely mentions "secrets" - the exact
    false-positive shape round 32's own judgment already rejected a wider
    glob for."""
    (tmp_path / "secrets-rotation-policy.md").write_bytes(b"# rotate secrets quarterly\n")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert {f.relative_path for f in result.files} == {"secrets-rotation-policy.md"}
    assert result.exclusions.get("secret", 0) == 0


def test_enumerate_scope_does_not_over_exclude_a_plausible_false_positive(tmp_path: Path) -> None:
    """FIX ROUND 32 (F5's own weighed disposition): ``secrets.properties``
    is matched as an EXACT LITERAL, deliberately never a wildcard - a
    harmless, unrelated file that merely mentions "secret" in its own name
    (documentation, never actual secret material) must stay a real,
    modeled file, not disappear into the same bucket."""
    (tmp_path / "credentials.md").write_bytes(b"# how to obtain credentials\n")
    (tmp_path / "docs-about-secrets.txt").write_bytes(b"not a secret\n")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert {f.relative_path for f in result.files} == {"credentials.md", "docs-about-secrets.txt"}
    assert result.exclusions.get("secret", 0) == 0


def test_enumerate_scope_no_longer_excludes_secretsjava_as_a_secret(tmp_path: Path) -> None:
    """FIX ROUND 37 (thirty-first cold read, F2 BLOCKER, wrong-data,
    .cr31-secretname verbatim): round 35's own ``secrets.*`` GLOB matched
    case-insensitively on Windows (``fnmatch.fnmatch`` applies
    ``os.path.normcase``) - so it also matched ``Secrets.java``, a real,
    parseable, adapter-handled JAVA SOURCE FILE, silently dropping it as
    category "secret" (its own GET route would vanish, complete/0
    problems). Narrowed to a closed extension list that never includes
    ``.java`` at all - this file is no longer excluded, period."""
    (tmp_path / "Secrets.java").write_bytes(
        b"package p;\nclass Secrets { void get() {} }\n")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert {f.relative_path for f in result.files} == {"Secrets.java"}
    assert result.exclusions.get("secret", 0) == 0


def test_enumerate_scope_canary_inversion_is_fixed(tmp_path: Path) -> None:
    """FIX ROUND 37 (F2 BLOCKER, .cr31-canary verbatim): the reader's own
    measured inversion - the REAL credentials.properties was INCLUDED
    (not on the closed list) while Secrets.java (not a credential at
    all) was EXCLUDED, purely because of Windows' own case-insensitive
    fnmatch. Both directions checked together: the genuine secret-shaped
    literal stays excluded, the ordinary Java source stays included."""
    (tmp_path / "Secrets.java").write_bytes(b"package p;\nclass Secrets {}\n")
    (tmp_path / "credentials").write_bytes(b"user:pass\n")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert {f.relative_path for f in result.files} == {"Secrets.java"}
    assert result.exclusions.get("secret", 0) == 1


def test_matches_any_secret_pattern_is_case_insensitive_on_every_platform() -> None:
    """FIX ROUND 37 (F2 BLOCKER, part 2 - the case policy; also answers
    F9's cross-platform-inventory-divergence note): unit-tested directly
    against the predicate, per this arc's own established discipline for
    a platform-sensitive detector (round 36's own F4 caveat) - a real
    filesystem's own case behavior is not something to assert against.
    Deliberately decided case-insensitive, identically on every platform:
    an UPPERCASE spelling of a genuine secret-shaped name is exactly as
    sensitive as its lowercase form."""
    assert discovery._matches_any_secret_pattern("id_rsa") is True
    assert discovery._matches_any_secret_pattern("ID_RSA") is True
    assert discovery._matches_any_secret_pattern("Id_Rsa") is True
    assert discovery._matches_any_secret_pattern(".env") is True
    assert discovery._matches_any_secret_pattern(".ENV") is True
    assert discovery._matches_any_secret_pattern("secrets.yaml") is True
    assert discovery._matches_any_secret_pattern("SECRETS.YAML") is True
    assert discovery._matches_any_secret_pattern("Secrets.java") is False
    assert discovery._matches_any_secret_pattern("SECRETS.JAVA") is False


def test_generated_vendor_dir_name_matching_is_case_sensitive_declared_asymmetry() -> None:
    """FIX ROUND 37 (F9 LOW, carry - folded into F2's own case policy):
    unlike F2's own secret-pattern matching (now deliberately case-
    insensitive), the generated/vendor directory-name predicate is
    matched via a plain `name in {...}` test - case-SENSITIVE on every
    platform, a real, declared asymmetry, not an oversight. Unit-tested
    directly against the predicate (explicit strings, no real
    filesystem) - a real "Target"-vs-"target" directory PAIR cannot
    even be constructed on this dev host's own case-insensitive NTFS
    filesystem to prove anything about real per-OS enumeration order
    either way, the identical constraint the F2/F4 caveats already name
    for this same class of check."""
    assert discovery._exclusion_category("target", "target", is_dir=True) == "generated_or_vendor"
    assert discovery._exclusion_category("Target", "Target", is_dir=True) is None
    assert discovery._exclusion_category("TARGET", "TARGET", is_dir=True) is None


def test_enumerate_scope_records_a_visible_degrading_problem_for_a_calibrated_collision(
    tmp_path: Path, monkeypatch,
) -> None:
    """FIX ROUND 37 (F2 BLOCKER, part 3 - THE CALIBRATION RULE): any
    secret-pattern hit on a genuinely adapter-handled extension must
    record a visible, degrading problem instead of a silent exclusion -
    a defense-in-depth structural rule for whatever FUTURE secret
    pattern collides with an adapter-handled extension, not only
    today's ``secrets.*`` shape (already closed off by part 1). Forces
    the condition directly via a synthetic pattern, the same technique
    this whole arc already uses to test a structural rule independent
    of which concrete pattern triggers it."""
    monkeypatch.setattr(discovery, "_SECRET_FILE_PATTERNS", ("secretcalibration.java",))
    (tmp_path / "secretcalibration.java").write_bytes(b"package p;\nclass C {}\n")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.files == []
    assert result.exclusions.get("secret", 0) == 1
    assert result.degraded is True
    calibration_problems = [
        p for p in result.problems
        if p["reason_code"] == "secret_pattern_matched_code_bearing_file"]
    assert len(calibration_problems) == 1
    assert calibration_problems[0]["path"] == "secretcalibration.java"


def test_enumerate_scope_records_a_visible_non_degrading_problem_for_a_secret_xml_collision(
    tmp_path: Path,
) -> None:
    """FIX ROUND 38 (thirty-second cold read, F4 MINOR, .cr32-secretxml,
    wrong-data): round 37's own calibration rule above was ``.java``-
    only, but the closed secrets list's own literal entries reach past
    ``.java`` - a code-bearing Spring beans XML root named exactly
    ``secrets.xml`` (one of the list's own eleven exact literals) used
    to drop category=secret with a COMPLETE/0-problem run, while a
    byte-equivalent ``beans.xml`` (no name collision) degrades via the
    ordinary root-sniff/tier machinery - ``SECRET_PATTERNS_CAVEAT``'s own
    "never silently" sentence was not actually true for this member of
    its own closed list. Recorded now, but NOT degrading (unlike the
    ``.java`` case above): this producer excludes pre-read, so whether
    this specific file was genuinely code-bearing or ordinary config is
    unknowable without reading content this rule exists to never read -
    "record, don't guess," the round 26b precedent.

    FIX ROUND 47 (forty-first cold read, M3 MAJOR, wrong-data - THE
    BARE-TRUTHINESS SIBLING, .cr41-secretxml): `fingerprint_complete`
    used to be `not problems` - a bare truthiness check that treated
    even this explicitly non-degrading problem's mere PRESENCE as
    nulling the fingerprint (freshness permanently unknown for this
    run), the round-39 F2 exact class, never swept here. Now derived
    from `degrades_run` - this problem must NOT null the fingerprint."""
    (tmp_path / "secrets.xml").write_bytes(
        b"<beans><bean id=\"x\" class=\"com.acme.X\"/></beans>")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.files == []
    assert result.exclusions.get("secret", 0) == 1
    assert result.degraded is False
    calibration_problems = [
        p for p in result.problems
        if p["reason_code"] == "secret_pattern_matched_code_bearing_file"]
    assert len(calibration_problems) == 1
    assert calibration_problems[0]["path"] == "secrets.xml"
    assert result.fingerprint_complete is True
    assert result.whole_scope_fingerprint is not None


def test_enumerate_scope_a_genuinely_degrading_problem_still_nulls_the_fingerprint(
    tmp_path: Path,
) -> None:
    """FIX ROUND 47 (M3 MAJOR control, .cr41-binjava): the mirror case -
    a discovery-level problem that IS degrading (an unlistable
    directory, a resource cap - a genuine walked-content omission) must
    still null the fingerprint exactly as before this fix; only a
    problem explicitly marked non-degrading is exempted."""
    # A nesting depth past MAX_NESTING_DEPTH is a genuine walked-content
    # omission (the same resource_limit shape every other cap in this
    # module raises) - simpler to trigger deterministically here than
    # monkeypatching OS-level directory permissions.
    #
    # MICRO-ROUND 47c (CI red, portability): single-char directory names
    # ("a", not "d{i}") - 202 levels of "d{i}" grows to "d200"/"d201" (4
    # chars) per component, pushing this fixture's own worst-case
    # absolute path (runner temp prefix + 202 components) past macOS's
    # PATH_MAX (1024) and failing os.mkdir with OSError 63 in SETUP,
    # before any assertion ever ran, on all four macOS CI legs (Linux's
    # 4096 and Windows's long-path-enabled limit both had headroom to
    # spare). 202 levels of "/a" is ~404 bytes - comfortably under 1000
    # even with a ~100-byte CI runner temp prefix.
    current = tmp_path
    for _ in range(discovery.MAX_NESTING_DEPTH + 2):
        current = current / "a"
    current.mkdir(parents=True)
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert any(p["reason_code"] == "resource_limit" for p in result.problems)
    assert result.fingerprint_complete is False
    assert result.whole_scope_fingerprint is None


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
    bounded-problem exit in this module.

    FIX ROUND 47 (forty-first cold read, B1 BLOCKER): the parse now
    delegates entirely to a ``git config -f`` subprocess (see
    ``_submodule_boundary_paths``'s own docstring) - a real permission-
    denied file makes THAT subprocess exit non-zero, never reachable by
    monkeypatching ``Path.read_text`` (no longer called at all). Same
    disposition simulated at its own new boundary instead."""
    (tmp_path / ".gitmodules").write_text(
        '[submodule "lib"]\n\tpath = lib\n\turl = https://example.invalid/lib.git\n',
        encoding="utf-8",
    )
    submodule_dir = tmp_path / "lib"
    submodule_dir.mkdir()
    (submodule_dir / "inner.txt").write_bytes(b"should never be enumerated")

    real_run = subprocess.run

    def _run(args, *pos_args, **kwargs):
        if args[:2] == ["git", "config"]:
            return subprocess.CompletedProcess(
                args, returncode=128, stdout="", stderr="fatal: unable to read config file")
        return real_run(args, *pos_args, **kwargs)

    monkeypatch.setattr(discovery.subprocess, "run", _run)
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


def test_git_binary_absent_for_gitmodules_records_a_problem_and_marks_incomplete(
    tmp_path: Path, monkeypatch,
) -> None:
    """FIX ROUND 47 (forty-first cold read, B1 BLOCKER): a genuinely
    absent git binary (FileNotFoundError from subprocess.run itself,
    never reaching a CompletedProcess at all) must get the SAME fail-
    open treatment as any other git-invocation failure - never a crash,
    never a silent empty boundary set. Deliberately NOT routed through
    the privacy preflight's own no-VCS disposition (a different
    question - this call only ever needs the git BINARY, never proof
    that `root` itself is a git worktree)."""
    (tmp_path / ".gitmodules").write_text(
        '[submodule "lib"]\n\tpath = lib\n\turl = https://example.invalid/lib.git\n',
        encoding="utf-8",
    )
    (tmp_path / "lib").mkdir()

    def _run(*_args, **_kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(discovery.subprocess, "run", _run)
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert any(p["reason_code"] == "parse_failed" and p["path"] == ".gitmodules"
               for p in result.problems)
    assert result.fingerprint_complete is False


def test_gitmodules_a_core_path_key_never_fabricates_a_submodule_boundary(
    tmp_path: Path,
) -> None:
    """FIX ROUND 47 (forty-first cold read, B1 BLOCKER, wrong-data,
    .cr41-gmspur - THE WORST FAILURE SHAPE): the old hand-rolled parse
    matched ANY line whose key (after stripping) was exactly "path",
    with no section scope at all - a [core] block's own `path = svc`
    key (real, unrelated git config, no reason it could not sit in this
    same file) was read identically to a real submodule declaration,
    fabricating a boundary at `svc/` - the REAL module `svc/` was then
    silently DELETED from the inventory on a complete/zero-problem run.
    `git config -f --list` reads this line as `core.path`, never
    `submodule.*.path` - proven here: `svc/` must be enumerated
    normally, not excluded as a boundary."""
    (tmp_path / ".gitmodules").write_text("[core]\n\tpath = svc\n", encoding="utf-8")
    svc_dir = tmp_path / "svc"
    svc_dir.mkdir()
    (svc_dir / "inner.txt").write_bytes(b"must be enumerated normally - not a submodule")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert result.boundaries == []
    assert sorted(f.relative_path for f in result.files) == [".gitmodules", "svc/inner.txt"]


def test_gitmodules_a_quoted_path_value_still_excludes_the_real_directory(
    tmp_path: Path,
) -> None:
    """FIX ROUND 47 (B1 BLOCKER, wrong-data, .cr41-gmquote - the mirror
    LEAKAGE direction): git reads a quoted path value (`path =
    "libs/foo"`) as the unquoted string `libs/foo` - the old hand-
    rolled parse's own bare `.strip()` left the surrounding quote
    characters IN the value, which then never matched the real on-disk
    directory, silently never excluding it (a genuine external
    submodule's own source published as first-party units)."""
    (tmp_path / ".gitmodules").write_text(
        '[submodule "foo"]\n\tpath = "libs/foo"\n\turl = https://example.invalid/foo.git\n',
        encoding="utf-8",
    )
    foo_dir = tmp_path / "libs" / "foo"
    foo_dir.mkdir(parents=True)
    (foo_dir / "inner.txt").write_bytes(b"should never be enumerated")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert len(result.boundaries) == 1
    assert result.boundaries[0].relative_path == "libs/foo"
    assert result.boundaries[0].boundary_kind == "submodule"
    assert not any(f.relative_path.startswith("libs/foo/") for f in result.files)


def test_gitmodules_a_subsection_name_containing_an_equals_sign_still_excludes_the_directory(
    tmp_path: Path,
) -> None:
    """FIX ROUND 48 (forty-second cold read, N1, judged - taken): a
    legal git-config subsection name containing its OWN literal ``=``
    (``[submodule "a=b"]``, a real if unusual spelling) makes plain
    ``git config --list`` emit ``submodule.a=b.path=libs/foo`` as ONE
    line with TWO ``=`` characters - splitting on the FIRST one (the
    old approach) reads the key as ``submodule.a`` (never ending in
    ``.path``), silently never recognizing this genuine submodule path
    and reopening the exact LEAKAGE direction round 47's own fix closed
    (a real external submodule published as first-party). ``-z``
    (NUL-separated entries, key/value split on the first NEWLINE
    instead) is unambiguous regardless of what the key or value
    contains."""
    (tmp_path / ".gitmodules").write_text(
        '[submodule "a=b"]\n\tpath = libs/foo\n\turl = https://example.invalid/foo.git\n',
        encoding="utf-8",
    )
    foo_dir = tmp_path / "libs" / "foo"
    foo_dir.mkdir(parents=True)
    (foo_dir / "inner.txt").write_bytes(b"should never be enumerated")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert len(result.boundaries) == 1
    assert result.boundaries[0].relative_path == "libs/foo"
    assert result.boundaries[0].boundary_kind == "submodule"
    assert not any(f.relative_path.startswith("libs/foo/") for f in result.files)


def test_gitmodules_a_trailing_slash_path_value_still_excludes_the_real_directory(
    tmp_path: Path,
) -> None:
    """FIX ROUND 47 (B1 BLOCKER, wrong-data, .cr41-gmslash): a trailing
    slash (`path = libs/foo/`, a real, legal git-config spelling) is
    preserved verbatim by git's own parser - stripped here so the
    boundary set still exact-matches the enumerator's own trailing-
    slash-free relative paths, rather than silently leaking."""
    (tmp_path / ".gitmodules").write_text(
        '[submodule "foo"]\n\tpath = libs/foo/\n\turl = https://example.invalid/foo.git\n',
        encoding="utf-8",
    )
    foo_dir = tmp_path / "libs" / "foo"
    foo_dir.mkdir(parents=True)
    (foo_dir / "inner.txt").write_bytes(b"should never be enumerated")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert len(result.boundaries) == 1
    assert result.boundaries[0].relative_path == "libs/foo"
    assert not any(f.relative_path.startswith("libs/foo/") for f in result.files)


def test_gitmodules_a_trailing_inline_comment_still_excludes_the_real_directory(
    tmp_path: Path,
) -> None:
    """FIX ROUND 47 (B1 BLOCKER, wrong-data, .cr41-gmcmt): a trailing
    inline comment (`path = libs/foo ; note`, a real, legal git-config
    spelling) is stripped by git's own parser before this producer ever
    sees the value - the old hand-rolled parse's own bare `.strip()`
    left the comment text IN the value, never matching the real
    directory."""
    (tmp_path / ".gitmodules").write_text(
        '[submodule "foo"]\n\tpath = libs/foo ; legacy mirror\n'
        "\turl = https://example.invalid/foo.git\n",
        encoding="utf-8",
    )
    foo_dir = tmp_path / "libs" / "foo"
    foo_dir.mkdir(parents=True)
    (foo_dir / "inner.txt").write_bytes(b"should never be enumerated")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert len(result.boundaries) == 1
    assert result.boundaries[0].relative_path == "libs/foo"
    assert not any(f.relative_path.startswith("libs/foo/") for f in result.files)


def test_gitmodules_an_ordinary_unquoted_path_is_unaffected(tmp_path: Path) -> None:
    """FIX ROUND 47 (B1 BLOCKER control, .cr41-gmnorm): the ordinary,
    dominant real-world shape (a bare, unquoted path, no trailing slash
    or comment) must keep excluding the real directory exactly as
    before this fix."""
    (tmp_path / ".gitmodules").write_text(
        '[submodule "foo"]\n\tpath = libs/foo\n\turl = https://example.invalid/foo.git\n',
        encoding="utf-8",
    )
    foo_dir = tmp_path / "libs" / "foo"
    foo_dir.mkdir(parents=True)
    (foo_dir / "inner.txt").write_bytes(b"should never be enumerated")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    assert len(result.boundaries) == 1
    assert result.boundaries[0].relative_path == "libs/foo"
    assert not any(f.relative_path.startswith("libs/foo/") for f in result.files)


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


# ------------------- F3 (round 29, twenty-fifth cold read): non-hard-excluded
# categories now contribute to the fingerprint too

def test_whole_scope_fingerprint_changes_when_a_binary_excluded_files_content_changes(
    tmp_path: Path,
) -> None:
    """FIX ROUND 29 (twenty-fifth cold read, F3 MAJOR, wrong-data): a
    changed binary-excluded file used to leave whole_scope_fingerprint
    completely UNCHANGED, even though the same run publishes its own
    changed content_digest in modules.json (round 28b's own binary-twin-
    unit fix) - a real, silent gap. The fingerprint must change when a
    binary-excluded file's own content changes."""
    (tmp_path / "logback.xml").write_bytes(b"<config>\x00v1</config>")
    comp_dir = _comprehension_dir(tmp_path)
    before = discovery.enumerate_scope(tmp_path, comp_dir)
    assert any(e["category"] == "binary" for e in before.excluded_roots)
    (tmp_path / "logback.xml").write_bytes(b"<config>\x00v2</config>")
    after = discovery.enumerate_scope(tmp_path, comp_dir)
    assert before.whole_scope_fingerprint != after.whole_scope_fingerprint


def test_an_oversized_files_excluded_roots_entry_carries_no_content_digest(
    tmp_path: Path, monkeypatch,
) -> None:
    """FIX ROUND 29 (F3 MAJOR, sweep): the docstring's own "same
    treatment applies to generated/vendor/oversize" - measured for the
    resource_limit_oversized category too. Unlike binary/
    resource_limit_total_bytes, NO content_digest is possible here at
    all (excluded from stat().st_size alone, BEFORE any read, by design
    - reading the file just to fingerprint it would reopen the exact
    resource-exhaustion risk this cap exists to prevent). This category
    also always records its own degrading `resource_limit` problem, so
    (like resource_limit_total_bytes) `whole_scope_fingerprint` itself
    is always `None` whenever it fires - asserted directly against the
    excluded_roots entry instead, the one fact this fix's own docstring
    claims (path+category only, digest deliberately absent)."""
    monkeypatch.setattr(discovery, "MAX_PER_FILE_BYTES", 4)
    (tmp_path / "big.bin").write_bytes(b"12345")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    entry = next(e for e in result.excluded_roots if e["category"] == "resource_limit_oversized")
    assert "content_digest" not in entry
    # FIX ROUND 47 (forty-first cold read, M7 MAJOR, corrected): the
    # docstring's own claim, now also code-verified directly - the
    # FINGERPRINT_CAVEAT's own former discussion of this category's
    # fingerprint-sensitivity GRANULARITY was dead code, since the
    # fingerprint is unconditionally absent, never partially present.
    assert result.fingerprint_complete is False
    assert result.whole_scope_fingerprint is None


def test_a_total_bytes_capped_files_excluded_roots_entry_carries_its_content_digest(
    tmp_path: Path, monkeypatch,
) -> None:
    """FIX ROUND 29 (F3 MAJOR, sweep): the resource_limit_total_bytes
    twin - bytes ARE already read here (the cap is checked AFTER
    reading, unlike resource_limit_oversized), so a real content_digest
    is carried the same way binary's already is. This category always
    ALSO records a degrading `resource_limit` problem of its own
    (unrelated to this fix), so `whole_scope_fingerprint` itself is
    always `None` whenever it fires (fingerprint_complete=False) - the
    content_digest is asserted directly instead, the fact this fix
    actually adds and the one a future non-degrading consumer of this
    same category would need."""
    monkeypatch.setattr(discovery, "MAX_HASHED_TOTAL_BYTES", 3)
    (tmp_path / "over.txt").write_bytes(b"aaaa")
    comp_dir = _comprehension_dir(tmp_path)
    result = discovery.enumerate_scope(tmp_path, comp_dir)
    entry = next(e for e in result.excluded_roots if e["category"] == "resource_limit_total_bytes")
    assert entry["content_digest"] == hashlib.sha256(b"aaaa").hexdigest()
    # FIX ROUND 47 (M7 MAJOR, corrected): see the sibling assertion in
    # the resource_limit_oversized test above - the content_digest this
    # category DOES carry is never actually consulted for the
    # fingerprint, since the fingerprint is unconditionally absent
    # whenever this category fires at all.
    assert result.fingerprint_complete is False
    assert result.whole_scope_fingerprint is None


def test_whole_scope_fingerprint_changes_when_a_generated_or_vendor_directorys_own_path_changes(
    tmp_path: Path,
) -> None:
    """FIX ROUND 29 (F3 MAJOR, sweep): the generated_or_vendor twin - no
    per-file digest is possible here at all (a directory-level skip,
    never walked, so there is no enumerated content to digest without
    defeating the entire point of excluding it), but the fingerprint
    must still be sensitive to WHICH path was excluded, not merely how
    many. The pre-existing `exclusions` category+COUNT field would
    already make a brand-new generated/vendor exclusion change the
    fingerprint on its own (1 vs 0) - too weak a test to prove this
    fix's own path-level addition actually did anything. Isolated here:
    renaming the excluded directory keeps the count identical (exactly
    one generated_or_vendor exclusion, either way) while the PATH
    itself changes - only this fix's own per-entry path/category tuple
    can tell the two scans apart."""
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "output.class").write_bytes(b"compiled")
    comp_dir = _comprehension_dir(tmp_path)
    before = discovery.enumerate_scope(tmp_path, comp_dir)
    assert before.exclusions.get("generated_or_vendor") == 1
    (tmp_path / "target").rename(tmp_path / "build")
    after = discovery.enumerate_scope(tmp_path, comp_dir)
    assert after.exclusions.get("generated_or_vendor") == 1
    assert before.whole_scope_fingerprint != after.whole_scope_fingerprint


def test_whole_scope_fingerprint_is_unaffected_by_a_secret_files_content(
    tmp_path: Path,
) -> None:
    """Companion control: secret/VCS/dependency-cache exclusions stay OUT
    of the fingerprint entirely, unchanged - the design's own "not read
    or copied into the fingerprint" wording for exactly these
    categories (confidentiality for secrets; cost for VCS/dependency-
    cache directories genuinely never worth walking at all)."""
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / ".env").write_bytes(b"SECRET=v1")
    comp_dir = _comprehension_dir(tmp_path)
    before = discovery.enumerate_scope(tmp_path, comp_dir)
    assert any(e["category"] == "secret" for e in before.excluded_roots)
    (tmp_path / ".env").write_bytes(b"SECRET=v2-completely-different-length-too")
    after = discovery.enumerate_scope(tmp_path, comp_dir)
    assert before.whole_scope_fingerprint == after.whole_scope_fingerprint


def test_whole_scope_fingerprint_changes_when_a_dependency_cache_directory_appears(
    tmp_path: Path,
) -> None:
    """MICRO-ROUND 30b (reviewer-3 delta on round 30's own F3, R3, wrong-
    data - correct before merge): dependency_cache contributes no PER-
    ENTRY input to the fingerprint (no individual path/digest for it
    ever joins the fingerprint's own input) - but its own CATEGORY TALLY
    does, via the pre-existing `exclusions` category+count field, the
    SAME mechanism the generated_or_vendor-count-alone test above
    already relies on. A whole new dependency_cache region appearing
    changes that category's own count, which changes the fingerprint -
    the round-30 F3 caveat's FIRST version wrongly claimed this
    category has NO fingerprint sensitivity at all, measured false."""
    (tmp_path / "a.txt").write_bytes(b"hello")
    comp_dir = _comprehension_dir(tmp_path)
    before = discovery.enumerate_scope(tmp_path, comp_dir)
    assert before.exclusions.get("dependency_cache") is None
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_bytes(b"module.exports = {};")
    after = discovery.enumerate_scope(tmp_path, comp_dir)
    assert after.exclusions.get("dependency_cache") == 1
    assert before.whole_scope_fingerprint != after.whole_scope_fingerprint


def test_whole_scope_fingerprint_changes_when_a_dependency_cache_directory_disappears(
    tmp_path: Path,
) -> None:
    """MICRO-ROUND 30b (R3): the reverse direction - measured, not just
    assumed symmetric with the appearance case above."""
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_bytes(b"module.exports = {};")
    comp_dir = _comprehension_dir(tmp_path)
    before = discovery.enumerate_scope(tmp_path, comp_dir)
    assert before.exclusions.get("dependency_cache") == 1
    shutil.rmtree(tmp_path / "node_modules")
    after = discovery.enumerate_scope(tmp_path, comp_dir)
    assert after.exclusions.get("dependency_cache") is None
    assert before.whole_scope_fingerprint != after.whole_scope_fingerprint


def test_whole_scope_fingerprint_is_unaffected_by_content_changes_inside_an_existing_dependency_cache_directory(
    tmp_path: Path,
) -> None:
    """MICRO-ROUND 30b (R3 control): the content-change half of the
    caveat's own claim, which the appearance/disappearance tests above
    do not exercise - an existing dependency_cache region's own count
    stays unchanged when a file inside it is modified (this producer
    never walks into it at all, so there is nothing to read in the
    first place), so the fingerprint stays byte-identical too."""
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_bytes(b"module.exports = {};")
    comp_dir = _comprehension_dir(tmp_path)
    before = discovery.enumerate_scope(tmp_path, comp_dir)
    (tmp_path / "node_modules" / "pkg.js").write_bytes(b"module.exports = { totally: 'different' };")
    after = discovery.enumerate_scope(tmp_path, comp_dir)
    assert before.exclusions.get("dependency_cache") == after.exclusions.get("dependency_cache") == 1
    assert before.whole_scope_fingerprint == after.whole_scope_fingerprint


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
