"""No tracked text file is stored with Windows line endings (#723).

A tracked file stored with CR makes a clean checkout look different to tools that convert line ends, and it
trips up `janitor --release`. This runs `git ls-files --eol` once. It is skipped when the tests run outside a
Git checkout (an unpacked sdist or an installed wheel has no tracked files to look at)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ALLOWED: frozenset[str] = frozenset()  # a deliberate CRLF fixture goes here, with the reason, and nowhere else


def test_no_tracked_text_file_is_stored_with_windows_line_endings() -> None:
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("not a Git checkout: an unpacked sdist or an installed wheel has no tracked files to check")
    done = subprocess.run(
        [git, "-C", str(ROOT), "ls-files", "--eol", "-z"], capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    if done.returncode:
        pytest.skip(f"git ls-files could not run here: {done.stderr.strip()[:200]}")
    bad = []
    for entry in done.stdout.split("\0"):
        info, _, name = entry.partition("\t")
        if info.startswith(("i/crlf", "i/mixed")) and name not in ALLOWED:
            bad.append(f"{name} ({info.split()[0]})")
    assert not bad, (
        "tracked text files are stored with Windows line endings: "
        + ", ".join(bad)
        + ". To fix a file, rewrite it with LF line endings only (for example in an editor, or with "
        "`sed -i 's/\\r$//' <file>`) and commit it; check with `git ls-files --eol <file>`, which must show i/lf. "
        "If a file must keep CRLF on purpose, add its path to ALLOWED in this test with the reason."
    )
