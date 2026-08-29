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


def test_direct_construction_of_preflight_result_is_rejected() -> None:
    """reviewer-1 cold-read finding 1 on PR-A, rq-6cc5560b62f6: the dataclass
    must not be publicly constructible despite its docstring claim."""
    with pytest.raises(TypeError):
        privacy.PrivacyPreflightResult(
            vcs_privacy="ignored", vcs_kind="git", matched_rule=None, work_id=None,
            root_binding="deadbeef",
        )
