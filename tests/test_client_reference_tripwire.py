"""Tests for scripts/client_reference_tripwire.py (task #216).

Deliberately never uses the two REAL denylisted strings from #215 - these
tests build their own throwaway denylist from harmless fixture words, so
the test suite itself can never become a third place those strings live in
plaintext (beyond the two already-swept locations from #215's own report).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "client_reference_tripwire.py"
_spec = importlib.util.spec_from_file_location("client_reference_tripwire", _MODULE_PATH)
tripwire = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tripwire
_spec.loader.exec_module(tripwire)


SALT_A = b"test-salt-alpha"
SALT_B = b"test-salt-bravo"
FIXTURE_STRINGS = ["fixtureword", "abcd"]  # stand-ins for the real denylist, len 11 and 4


def _config(salt: bytes = SALT_A, min_ngram: int = 4) -> "tripwire.TripwireConfig":
    return tripwire.build_denylist(FIXTURE_STRINGS, salt, min_ngram=min_ngram)


# --------------------------------------------------------- build / (de)serialize

def test_build_denylist_hash_count_matches_ngram_math():
    # "abcd" (len 4) at floor 4 contributes exactly 1 hash (itself).
    # "fixtureword" (len 11) at floor 4 contributes sum_{L=4..11} (11-L+1)
    # = 8+7+6+5+4+3+2+1 = 36. Total 37, assuming no accidental collisions.
    config = _config()
    assert len(config.hashes) == 37


def test_denylist_round_trips_through_json(tmp_path: Path):
    config = _config()
    path = tmp_path / "denylist.json"
    tripwire.save_denylist(config, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["algorithm"] == "hmac-sha256"
    assert set(raw["hashes"]) == config.hashes
    reloaded = tripwire.load_denylist(path)
    assert reloaded.hashes == config.hashes
    assert reloaded.min_ngram == config.min_ngram
    assert reloaded.max_ngram == config.max_ngram


def test_denylist_file_never_contains_the_raw_strings(tmp_path: Path):
    """The whole point of the mechanism: the committed artifact must not
    let a plain-text search recover the denylisted strings."""
    config = _config()
    path = tmp_path / "denylist.json"
    tripwire.save_denylist(config, path)
    raw_text = path.read_text(encoding="utf-8")
    for s in FIXTURE_STRINGS:
        assert s not in raw_text
        assert s.upper() not in raw_text


# --------------------------------------------------------------- find_hits

def test_find_hits_detects_exact_occurrence():
    config = _config()
    hits = tripwire.find_hits("a document mentioning fixtureword here", config, SALT_A)
    assert len(hits) >= 1


def test_find_hits_is_case_insensitive():
    config = _config()
    hits_lower = tripwire.find_hits("fixtureword", config, SALT_A)
    hits_upper = tripwire.find_hits("FIXTUREWORD", config, SALT_A)
    hits_mixed = tripwire.find_hits("FixtureWord", config, SALT_A)
    assert len(hits_lower) == len(hits_upper) == len(hits_mixed) > 0


def test_find_hits_clean_text_has_no_hits():
    config = _config()
    hits = tripwire.find_hits("nothing suspicious in this sentence at all", config, SALT_A)
    assert hits == []


def test_find_hits_requires_the_matching_salt():
    """Mutation-style proof the salt actually gates matching, not just the
    denylist's presence: the SAME text with the WRONG salt must not match."""
    config = _config(salt=SALT_A)
    hits_right_salt = tripwire.find_hits("fixtureword", config, SALT_A)
    hits_wrong_salt = tripwire.find_hits("fixtureword", config, SALT_B)
    assert hits_right_salt != []
    assert hits_wrong_salt == []


def test_find_hits_reports_location_not_content():
    """Hit objects must never carry the matched substring - only where it
    is - so a report can never echo the denylisted content back out."""
    hit = tripwire.Hit(line=1, column=1, length=4, offset=0)
    assert not hasattr(hit, "text")
    assert not hasattr(hit, "matched")
    assert "fixtureword" not in repr(hit)


def test_find_hits_line_column_point_at_the_real_spot():
    config = _config()
    text = "line one is clean\nline two has fixtureword in it\n"
    hits = tripwire.dedupe_overlapping(tripwire.find_hits(text, config, SALT_A))
    assert len(hits) == 1
    assert hits[0].line == 2
    # "fixtureword" starts at column 13 on line 2 (1-based).
    assert hits[0].column == text.splitlines()[1].index("fixtureword") + 1


# ------------------------------------------------------------ dedupe_overlapping

def test_dedupe_merges_a_hyphenated_compounds_two_valid_matches_into_one():
    # "fixtureword-abcd": boundary alignment makes BOTH the leading
    # "fixtureword" (full word, right-bounded by the hyphen) and the
    # trailing "abcd" (full word, left-bounded by the hyphen) valid,
    # independent hits - real, DISTINCT occurrences sharing one compound,
    # not the same occurrence double-counted. Dedup must not collapse them.
    config = _config()
    raw_hits = tripwire.find_hits("prefix fixtureword-abcd suffix", config, SALT_A)
    assert len(raw_hits) == 2
    merged = tripwire.dedupe_overlapping(raw_hits)
    assert len(merged) == 2


def test_find_hits_rejects_a_denylisted_fragment_buried_in_an_unrelated_word():
    """round-2 (empirical, found on this project's own remediation text):
    a short floor-length n-gram of a longer denylisted string can be a
    substring of a completely unrelated, innocent word by pure coincidence
    of spelling. Word-boundary alignment must reject that, or the tripwire
    false-positives on ordinary prose forever."""
    config = _config()  # denylist includes "fixtureword" (contains "ture")
    hits = tripwire.find_hits("the departure schedule is unrelated to this", config, SALT_A)
    assert hits == []  # "departure" contains "ture" but is not the denylisted word


def test_dedupe_keeps_two_separate_occurrences_separate():
    config = _config()
    text = "fixtureword ... a lot of unrelated padding text goes here ... fixtureword"
    hits = tripwire.dedupe_overlapping(tripwire.find_hits(text, config, SALT_A))
    assert len(hits) == 2


# ------------------------------------------------------------------ resolve_salt

def test_resolve_salt_prefers_env_over_local_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    local_file = tmp_path / "salt-file"
    local_file.write_text("from-file", encoding="utf-8")
    monkeypatch.setenv(tripwire.SALT_ENV_VAR, "from-env")
    assert tripwire.resolve_salt(local_file) == b"from-env"


def test_resolve_salt_falls_back_to_local_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(tripwire.SALT_ENV_VAR, raising=False)
    local_file = tmp_path / "salt-file"
    local_file.write_text("from-file\n", encoding="utf-8")
    assert tripwire.resolve_salt(local_file) == b"from-file"


def test_resolve_salt_none_when_neither_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(tripwire.SALT_ENV_VAR, raising=False)
    assert tripwire.resolve_salt(tmp_path / "does-not-exist") is None


def test_resolve_salt_none_when_local_file_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(tripwire.SALT_ENV_VAR, raising=False)
    local_file = tmp_path / "salt-file"
    local_file.write_text("   \n", encoding="utf-8")
    assert tripwire.resolve_salt(local_file) is None


# --------------------------------------------------------------------- CLI: check

def _run_cli(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603  # nosec B603 - fixed argv, test-only
        [sys.executable, str(_MODULE_PATH), *args],
        capture_output=True, text=True, env=env,
    )


def test_cli_check_exits_clean_on_clean_file(tmp_path: Path):
    denylist = tmp_path / "denylist.json"
    tripwire.save_denylist(_config(), denylist)
    salt_file = tmp_path / "salt"
    salt_file.write_text("test-salt-alpha", encoding="utf-8")
    target = tmp_path / "clean.txt"
    target.write_text("nothing to see here\n", encoding="utf-8")

    result = _run_cli(["--denylist", str(denylist), "--local-salt-file", str(salt_file),
                       "check", str(target)])
    assert result.returncode == tripwire.EXIT_CLEAN, result.stderr


def test_cli_check_exits_hit_on_dirty_file(tmp_path: Path):
    denylist = tmp_path / "denylist.json"
    tripwire.save_denylist(_config(), denylist)
    salt_file = tmp_path / "salt"
    salt_file.write_text("test-salt-alpha", encoding="utf-8")
    target = tmp_path / "dirty.txt"
    target.write_text("this mentions fixtureword right here\n", encoding="utf-8")

    result = _run_cli(["--denylist", str(denylist), "--local-salt-file", str(salt_file),
                       "check", str(target)])
    assert result.returncode == tripwire.EXIT_HIT, result.stderr
    assert "fixtureword" not in result.stdout  # never echo the match
    assert "fixtureword" not in result.stderr


def test_cli_check_exits_salt_unavailable_without_salt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(tripwire.SALT_ENV_VAR, raising=False)
    denylist = tmp_path / "denylist.json"
    tripwire.save_denylist(_config(), denylist)
    target = tmp_path / "clean.txt"
    target.write_text("irrelevant\n", encoding="utf-8")

    result = _run_cli(["--denylist", str(denylist),
                       "--local-salt-file", str(tmp_path / "does-not-exist"),
                       "check", str(target)])
    assert result.returncode == tripwire.EXIT_SALT_UNAVAILABLE
    assert "SALT UNAVAILABLE" in result.stderr


def test_cli_check_salt_unavailable_is_never_confused_with_clean(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch):
    """Mutation-style regression: EXIT_SALT_UNAVAILABLE must differ from
    EXIT_CLEAN even though both mean 'no hits were printed' - a caller that
    only checks `== 0` would silently treat a broken check as a pass."""
    assert tripwire.EXIT_SALT_UNAVAILABLE != tripwire.EXIT_CLEAN
    assert tripwire.EXIT_SALT_UNAVAILABLE != tripwire.EXIT_HIT


# ------------------------------------------------------------------ CLI: check-diff

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603,S607  # nosec B603 B607 - test-only fixed argv
        ["git", *args], cwd=root, capture_output=True, text=True, check=True,
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")


def test_check_diff_flags_only_added_lines_not_removed_ones(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    denylist = repo / "denylist.json"
    tripwire.save_denylist(_config(), denylist)
    salt_file = repo / "salt"
    salt_file.write_text("test-salt-alpha", encoding="utf-8")

    target = repo / "notes.txt"
    target.write_text("an old line mentioning fixtureword\nother content\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base with the bad word already present")

    # A commit that only REMOVES the bad line must not be flagged - the
    # tripwire exists to stop NEW introductions, per #215's "no history
    # rewrite" decision; it must not make removing old content look like a
    # failure, or nobody could ever clean one up.
    target.write_text("other content\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "remove the bad line")

    result = _run_cli(["--denylist", str(denylist), "--local-salt-file", str(salt_file),
                       "check-diff", "HEAD~1", "HEAD", "--repo", str(repo)])
    assert result.returncode == tripwire.EXIT_CLEAN, result.stdout + result.stderr


def test_check_diff_flags_a_newly_introduced_occurrence(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    denylist = repo / "denylist.json"
    tripwire.save_denylist(_config(), denylist)
    salt_file = repo / "salt"
    salt_file.write_text("test-salt-alpha", encoding="utf-8")

    target = repo / "notes.txt"
    target.write_text("clean content\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "clean base")

    target.write_text("clean content\nnow with fixtureword added\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "introduce the bad word")

    result = _run_cli(["--denylist", str(denylist), "--local-salt-file", str(salt_file),
                       "check-diff", "HEAD~1", "HEAD", "--repo", str(repo)])
    assert result.returncode == tripwire.EXIT_HIT, result.stdout + result.stderr


# ------------------------------------------------------ pre-commit hook (shell)

_HOOK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "githooks" / "pre-commit"


def _hook_repo(tmp_path: Path) -> Path:
    """A scratch git repo wired exactly like a real clone: the hook script,
    the tripwire module, a throwaway (fixture-word) denylist copied in
    place of the real one, and `core.hooksPath` pointed at it - the same
    setup a contributor's real clone would have, so this test exercises
    the ACTUAL shell script argv, not a paraphrase of it. This is what
    caught a real bug during development: the hook originally placed
    `--local-salt-file` AFTER the `check` subcommand, which argparse
    silently rejects (that flag is only defined on the top-level parser) -
    a Python-level test of the module never exercises the shell script's
    own argv construction, only a shell-level test does."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")

    (repo / "scripts").mkdir()
    shutil.copy(_MODULE_PATH, repo / "scripts" / "client_reference_tripwire.py")
    tripwire.save_denylist(_config(), repo / "scripts" / "client-reference-denylist.json")

    hooks_dir = repo / "scripts" / "githooks"
    hooks_dir.mkdir()
    hook_text = _HOOK_PATH.read_text(encoding="utf-8")
    (hooks_dir / "pre-commit").write_text(hook_text, encoding="utf-8", newline="\n")
    (hooks_dir / "pre-commit").chmod(0o755)
    _git(repo, "config", "core.hooksPath", "scripts/githooks")
    return repo


def _commit(repo: Path, content: str, message: str) -> subprocess.CompletedProcess:
    (repo / "f.txt").write_text(content, encoding="utf-8")
    _git(repo, "add", "f.txt")
    return subprocess.run(  # noqa: S603,S607  # nosec B603 B607 - test-only fixed argv
        ["git", "commit", "-q", "-m", message], cwd=repo, capture_output=True, text=True,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required for the hook test")
def test_precommit_hook_allows_a_clean_commit(tmp_path: Path):
    repo = _hook_repo(tmp_path)
    (repo / ".agenttalk-tripwire-salt").write_text("test-salt-alpha", encoding="utf-8")
    result = _commit(repo, "clean content\n", "clean")
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required for the hook test")
def test_precommit_hook_blocks_a_dirty_commit_when_salt_is_present(tmp_path: Path):
    repo = _hook_repo(tmp_path)
    (repo / ".agenttalk-tripwire-salt").write_text("test-salt-alpha", encoding="utf-8")
    result = _commit(repo, "a line mentioning fixtureword\n", "dirty")
    assert result.returncode != 0
    assert "BLOCKED" in result.stderr
    assert "fixtureword" not in result.stdout
    assert "fixtureword" not in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required for the hook test")
def test_precommit_hook_fails_open_when_salt_is_absent(tmp_path: Path):
    """The hook's OWN documented contract (see its header comment): a
    missing local salt must never block a commit - CI is the real gate."""
    repo = _hook_repo(tmp_path)
    # No .agenttalk-tripwire-salt written at all.
    result = _commit(repo, "a line mentioning fixtureword\n", "dirty but no local salt")
    assert result.returncode == 0, result.stderr
    assert "salt not configured locally" in result.stderr


