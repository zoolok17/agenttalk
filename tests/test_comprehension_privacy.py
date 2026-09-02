"""#55 slice-1 PR-A: VCS privacy preflight (DESIGN-55-comprehension-plane.md,
"Privacy and offline enforcement"). Exercises the REAL git binary against a
real repository — the design's own point is that ignore status must be
PROVEN by Git's own matcher, never guessed from file text, so these tests
prove the same way.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agenttalk.comprehension import privacy
from agenttalk.comprehension.errors import VcsPrivacyRefused

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for the privacy preflight tests")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8")


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


# ----------------------------------------------------------- proven ignored

def test_ignored_via_directory_gitignore_pattern(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")
    _commit_all(tmp_path, "base")
    result = privacy.run_privacy_preflight(tmp_path)
    assert result.vcs_privacy == "ignored"
    assert result.vcs_kind == "git"
    assert result.matched_rule is not None
    assert result.work_id is None


def test_ignored_via_specific_comprehension_subpath_pattern(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(
        ".agenttalk/comprehension/\n", encoding="utf-8")
    _commit_all(tmp_path, "base")
    result = privacy.run_privacy_preflight(tmp_path)
    assert result.vcs_privacy == "ignored"


def test_ignored_pattern_need_not_be_committed(tmp_path: Path) -> None:
    """Git's ignore matcher reads the WORKING .gitignore, not only a
    committed one — an uncommitted .gitignore still proves ignore status."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")
    result = privacy.run_privacy_preflight(tmp_path)
    assert result.vcs_privacy == "ignored"


# ----------------------------------------------------------- not proven ignored

def test_refuses_when_not_ignored(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit_all(tmp_path, "base")
    with pytest.raises(VcsPrivacyRefused) as exc_info:
        privacy.run_privacy_preflight(tmp_path)
    assert exc_info.value.vcs_kind == "git"


def test_refuses_when_gitignore_excludes_something_else(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
    _commit_all(tmp_path, "base")
    with pytest.raises(VcsPrivacyRefused):
        privacy.run_privacy_preflight(tmp_path)


def test_no_plane_output_is_written_on_refusal(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit_all(tmp_path, "base")
    with pytest.raises(VcsPrivacyRefused):
        privacy.run_privacy_preflight(tmp_path)
    assert not (tmp_path / ".agenttalk").exists()


# --------------------- FIX ROUND 32 (twenty-eighth cold read, F1 BLOCKER):
# a single probe at one synthetic depth generalizes its one answer to the
# whole store - these prove the multi-probe rewrite closes both measured
# defeat shapes while leaving the genuinely-safe and genuinely-refused
# shapes unchanged.

def test_shapeA_broad_rule_still_proven_ignored(tmp_path: Path) -> None:
    """Control: a genuinely safe, broad rule ignores every probe for
    real - unaffected by the multi-probe rewrite."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")
    _commit_all(tmp_path, "base")
    result = privacy.run_privacy_preflight(tmp_path)
    assert result.vcs_privacy == "ignored"


def test_shapeB_unrelated_rule_still_refused(tmp_path: Path) -> None:
    """Control: a rule that ignores nothing under the comprehension dir
    stays refused exactly as before."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
    _commit_all(tmp_path, "base")
    with pytest.raises(VcsPrivacyRefused):
        privacy.run_privacy_preflight(tmp_path)


def test_probe_negate_reinclusion_idiom_now_refused(tmp_path: Path) -> None:
    """A git re-inclusion idiom (ignore everything under .agenttalk/, then
    re-include comprehension/runs/**) made the OLD single ``.privacy-probe/
    probe.json`` sentinel come back "ignored" while the REAL published
    artifacts under runs/** stayed un-ignored and stageable by
    ``git add -A``. The multi-probe rewrite must now refuse - the runs/
    probe proves un-ignored."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(
        ".agenttalk/**\n"
        "!.agenttalk/comprehension/\n"
        "!.agenttalk/comprehension/runs/\n"
        "!.agenttalk/comprehension/runs/**\n",
        encoding="utf-8")
    _commit_all(tmp_path, "base")
    with pytest.raises(VcsPrivacyRefused):
        privacy.run_privacy_preflight(tmp_path)


def test_probe_shapeC_rule_scoped_to_old_probe_dir_now_refused(tmp_path: Path) -> None:
    """A rule matching ONLY the OLD probe's own private subdirectory
    unlocked the write under the single-probe mechanism (it never touched
    index.json/runs//.staging). The multi-probe rewrite no longer probes
    that path at all (only index.json / runs / .staging), so this rule
    proves nothing and every real probe proves un-ignored."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(
        "/.agenttalk/**/.privacy-probe/\n", encoding="utf-8")
    _commit_all(tmp_path, "base")
    with pytest.raises(VcsPrivacyRefused):
        privacy.run_privacy_preflight(tmp_path)


# ----------------------------------------------------------- already tracked (refuse even if ignored later)

def test_refuses_when_comprehension_dir_is_already_tracked(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    tracked = tmp_path / ".agenttalk" / "comprehension" / "index.json"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("{}", encoding="utf-8")
    _commit_all(tmp_path, "oops, committed the private store")
    # Even though nothing ELSE excludes it, the tracked-path check must fire
    # first and independently of the ignore-matcher outcome.
    with pytest.raises(VcsPrivacyRefused) as exc_info:
        privacy.run_privacy_preflight(tmp_path)
    assert exc_info.value.vcs_kind == "git"
    assert "tracked" in exc_info.value.detail


def test_tracked_check_fires_even_if_later_gitignored(tmp_path: Path) -> None:
    """A path already tracked stays tracked even after a later .gitignore
    entry is added — git ls-files still reports it, so the refusal must
    still fire (design: reject tracked FIRST, independent of the ignore
    matcher)."""
    _init_repo(tmp_path)
    tracked = tmp_path / ".agenttalk" / "comprehension" / "index.json"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("{}", encoding="utf-8")
    _commit_all(tmp_path, "oops")
    (tmp_path / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")
    _commit_all(tmp_path, "add gitignore too late")
    with pytest.raises(VcsPrivacyRefused):
        privacy.run_privacy_preflight(tmp_path)


# ----------------------------------------------------------- no VCS at all

def test_refuses_with_no_vcs_kind_when_there_is_no_git_repo(tmp_path: Path) -> None:
    with pytest.raises(VcsPrivacyRefused) as exc_info:
        privacy.run_privacy_preflight(tmp_path)
    assert exc_info.value.vcs_kind == "none"


def test_not_a_git_worktree_message_never_leaks_the_absolute_root(tmp_path: Path) -> None:
    """FIX ROUND 14 (tenth cold read, CR10-12 polish): this message
    reaches the CLI's plain stderr output - the one place in this whole
    plane that used to name the raw absolute local root next to a
    projection family that otherwise never persists one (scan.json's
    root_binding is a one-way digest specifically to avoid this)."""
    with pytest.raises(VcsPrivacyRefused) as exc_info:
        privacy.run_privacy_preflight(tmp_path)
    assert str(tmp_path) not in str(exc_info.value)
    assert tmp_path.name in str(exc_info.value)


# ----------------------------------------------------------- acknowledge_unignored_private_store

def test_acknowledge_records_acknowledged_unignored_for_git(tmp_path: Path) -> None:
    result = privacy.acknowledge_unignored_private_store(
        tmp_path, vcs_kind="git", work_id="migrate-checkout")
    assert result.vcs_privacy == "acknowledged_unignored"
    assert result.work_id == "migrate-checkout"


def test_acknowledge_records_no_vcs_acknowledged_for_no_vcs(tmp_path: Path) -> None:
    result = privacy.acknowledge_unignored_private_store(tmp_path, vcs_kind="none", work_id="w1")
    assert result.vcs_privacy == "no_vcs_acknowledged"


@pytest.mark.parametrize("work_id", ["", "   ", None])
def test_acknowledge_requires_a_non_empty_work_id(tmp_path: Path, work_id) -> None:
    with pytest.raises(VcsPrivacyRefused, match="work_id"):
        privacy.acknowledge_unignored_private_store(tmp_path, vcs_kind="git", work_id=work_id)


def test_acknowledge_rejects_an_unsupported_vcs_kind(tmp_path: Path) -> None:
    with pytest.raises(VcsPrivacyRefused, match="unsupported"):
        privacy.acknowledge_unignored_private_store(tmp_path, vcs_kind="svn", work_id="w1")


def test_acknowledge_carries_through_the_matched_rule_when_provided(tmp_path: Path) -> None:
    result = privacy.acknowledge_unignored_private_store(
        tmp_path, vcs_kind="git", work_id="w1", matched_rule=".gitignore:1:build/")
    assert result.matched_rule == ".gitignore:1:build/"


# --------------------- MICRO-ROUND 28b (R4): shared pairing predicate

def test_pairing_message_when_acknowledge_true_and_no_work_id() -> None:
    message = privacy.acknowledge_requires_work_id_message(
        acknowledge_unignored=True, work_id=None)
    assert message is not None
    assert "--work-id" in message


def test_pairing_message_when_acknowledge_true_and_empty_work_id() -> None:
    message = privacy.acknowledge_requires_work_id_message(
        acknowledge_unignored=True, work_id="")
    assert message is not None


def test_pairing_message_when_acknowledge_true_and_whitespace_only_work_id() -> None:
    """FIX ROUND 30 (twenty-sixth cold read, F5 polish, wrong-data): a
    whitespace-only work_id ("   ") used to pass this guard - a bare
    truthiness check treats any non-empty string as fine, and a string
    of only whitespace IS non-empty. --run's own identical shape
    (_resolve_run_id, round 29's own F7) already refuses whitespace-
    only explicitly; mirrored here."""
    message = privacy.acknowledge_requires_work_id_message(
        acknowledge_unignored=True, work_id="   ")
    assert message is not None
    assert "--work-id" in message


def test_pairing_message_is_none_when_acknowledge_true_and_work_id_present() -> None:
    assert privacy.acknowledge_requires_work_id_message(
        acknowledge_unignored=True, work_id="migrate-checkout") is None


def test_pairing_message_is_none_when_acknowledge_false_regardless_of_work_id() -> None:
    assert privacy.acknowledge_requires_work_id_message(
        acknowledge_unignored=False, work_id=None) is None
    assert privacy.acknowledge_requires_work_id_message(
        acknowledge_unignored=False, work_id="w1") is None


def test_direct_construction_of_preflight_result_is_rejected() -> None:
    """reviewer-1 cold-read finding 1 on PR-A, rq-6cc5560b62f6: the dataclass
    must not be publicly constructible despite its docstring claim."""
    with pytest.raises(TypeError):
        privacy.PrivacyPreflightResult(
            vcs_privacy="ignored", vcs_kind="git", matched_rule=None, work_id=None,
            root_binding="deadbeef",
        )


# --------------------- FIX ROUND 34 (reviewer-3's re-delta on round 33's
# own R1 fix - THE HOLE): verify_store_ignored, the store-wide ground-truth
# check that replaces per-directory enumeration entirely.

def test_preflight_still_refuses_a_static_index_json_only_reinclusion(tmp_path: Path) -> None:
    """Regression control (dispatch item 3): the PREFLIGHT's own probe
    already names the real ``index.json`` path literally (see
    ``_PRIVACY_PROBE_RELATIVE_PATHS``), so a STATIC rule (present from
    the very start, unlike the round 34 MID-RUN flip shapes above) that
    re-includes only ``index.json`` was already caught before round 34
    and must stay caught - round 34 only widens the POST-publish
    guarantee, it never narrows the preflight's own existing reach."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(
        ".agenttalk/**\n"
        "!.agenttalk/comprehension/\n"
        "!.agenttalk/comprehension/index.json\n",
        encoding="utf-8",
    )
    _commit_all(tmp_path, "base")
    with pytest.raises(VcsPrivacyRefused):
        privacy.run_privacy_preflight(tmp_path)


def test_verify_store_ignored_passes_when_the_whole_store_is_ignored(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")
    _commit_all(tmp_path, "base")
    store = tmp_path / ".agenttalk" / "comprehension"
    (store / "runs" / "scan-1").mkdir(parents=True)
    (store / "index.json").write_text("{}", encoding="utf-8")
    (store / "runs" / "scan-1" / "scan.json").write_text("{}", encoding="utf-8")
    privacy.verify_store_ignored(tmp_path, ".agenttalk/comprehension")  # must not raise


def test_verify_store_ignored_refuses_when_index_json_specifically_is_stageable(
    tmp_path: Path,
) -> None:
    """The reader's own THE HOLE shape: a rule re-including ONLY
    index.json (never touching runs/**) still makes it stageable - the
    whole-store check must catch this exactly as it catches a runs/**
    leak, since it never enumerates by directory at all."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(
        ".agenttalk/**\n"
        "!.agenttalk/comprehension/\n"
        "!.agenttalk/comprehension/index.json\n",
        encoding="utf-8",
    )
    _commit_all(tmp_path, "base")
    store = tmp_path / ".agenttalk" / "comprehension"
    (store / "runs" / "scan-1").mkdir(parents=True)
    (store / "index.json").write_text("{}", encoding="utf-8")
    (store / "runs" / "scan-1" / "scan.json").write_text("{}", encoding="utf-8")
    with pytest.raises(VcsPrivacyRefused, match="index.json"):
        privacy.verify_store_ignored(tmp_path, ".agenttalk/comprehension")


def test_verify_store_ignored_refuses_on_an_unanticipated_new_file(tmp_path: Path) -> None:
    """The class-closure proof: a file NO enumeration ever anticipated
    (never named by any probe, any prior fix, or any test fixture) must
    still be caught, because this check never enumerates anything - it
    asks git what is stageable, period."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(
        ".agenttalk/**\n"
        "!.agenttalk/comprehension/\n"
        "!.agenttalk/comprehension/a-file-nobody-named.txt\n",
        encoding="utf-8",
    )
    _commit_all(tmp_path, "base")
    store = tmp_path / ".agenttalk" / "comprehension"
    store.mkdir(parents=True)
    (store / "index.json").write_text("{}", encoding="utf-8")
    (store / "a-file-nobody-named.txt").write_text("surprise", encoding="utf-8")
    with pytest.raises(VcsPrivacyRefused, match="a-file-nobody-named.txt"):
        privacy.verify_store_ignored(tmp_path, ".agenttalk/comprehension")


def test_verify_store_ignored_fails_closed_when_not_a_git_repo(tmp_path: Path) -> None:
    """A genuine git query failure (no repository at all here) must
    refuse, never silently treat "the query broke" as "nothing is
    stageable" - fail-closed, the same discipline every other check in
    this module already follows."""
    store = tmp_path / ".agenttalk" / "comprehension"
    store.mkdir(parents=True)
    (store / "index.json").write_text("{}", encoding="utf-8")
    with pytest.raises(VcsPrivacyRefused):
        privacy.verify_store_ignored(tmp_path, ".agenttalk/comprehension")
