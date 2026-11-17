"""Inspection advice stays readable and appears only when needed."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agenttalk import janitor
from test_git_index_safety import safety


@pytest.mark.parametrize("folder", [r"C:\checkout folder\project", "/checkout folder/project"])
def test_guidance_shows_plain_checkout_and_literal_file_placeholder(monkeypatch, folder):
    monkeypatch.setattr(safety(), "_checkout_root", lambda root: folder)

    text = safety().inspection_guidance("nested/store")

    assert f"Checkout folder: {folder}." in text
    assert "update-index --no-assume-unchanged -- <file>" in text
    assert "update-index --no-skip-worktree -- <file>" in text
    assert "<file> is" not in text
    assert "nested/store" not in text


@pytest.mark.parametrize("dirty", [False, True])
@pytest.mark.parametrize("apply", [False, True])
def test_janitor_explains_dirty_only_when_report_has_dirty_lines(dirty, apply):
    cfg = SimpleNamespace(repo=Path("repo"), tmp_root=Path("tmp"), scratch_root=Path("scratch"))
    paths = [Path("checkout")] if dirty else []
    report = janitor.JanitorReport([], paths, paths, [], [])

    text = janitor.format_report(report, cfg, apply=apply)

    explanation = (
        "DIRTY includes checkouts that could not be fully checked; hidden tracked files are kept for inspection."
    )
    assert (explanation in text) is dirty
    assert ("  DIRTY checkout" in text) is dirty
