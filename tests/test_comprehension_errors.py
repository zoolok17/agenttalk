"""#55 slice-1: typed error hierarchy and bounded-detail helpers
(errors.py)."""

from __future__ import annotations

from agenttalk.comprehension import errors


def test_bounded_detail_leaves_a_short_string_unchanged():
    assert errors.bounded_detail("a short detail") == "a short detail"


def test_bounded_detail_at_exactly_the_bound_is_unchanged():
    text = "x" * errors.MAX_PROBLEM_DETAIL_LENGTH
    assert errors.bounded_detail(text) == text


def test_bounded_detail_truncates_with_a_visible_marker():
    """FIX ROUND 37 (thirty-first cold read, F6 LOW, wrong-data):
    bounded_detail used to slice at exactly MAX_PROBLEM_DETAIL_LENGTH
    with NO marker - silently truncating mid-word, indistinguishable
    from a detail that genuinely ends there. The same visible-
    truncation marker adapters.java._bounded_route_target already
    establishes for the identical shape."""
    text = "x" * (errors.MAX_PROBLEM_DETAIL_LENGTH + 50)
    result = errors.bounded_detail(text)
    assert result == "x" * errors.MAX_PROBLEM_DETAIL_LENGTH + "...(truncated)"
    assert result.endswith("...(truncated)")
