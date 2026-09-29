"""#223 / PR #228: direct unit tests for conftest.py's
``_run_privacy_preflight_with_bounded_retry`` - the helper behind the
``comprehension_privacy`` fixture that absorbs a transient
``VcsPrivacyRefused`` on a loaded CI host (a real ``git`` subprocess call
timing out on scheduling delay alone) without masking a genuine refusal.

Two fix rounds landed on this helper:

1. The ls-files/check-ignore probes timing out -> a "could not be
   trusted" ``VcsPrivacyRefused``.
2. PR #228: the FIRST git call (``rev-parse --is-inside-work-tree``)
   itself timing out -> a DIFFERENT message, "... is not inside a Git
   worktree (or git is unavailable)", which round 1's fix did not match
   at all.

None of these properties is provable by just running the real
comprehension suite on a fast, idle dev sandbox where the underlying
``git`` calls essentially never time out - this module drives the helper
directly with a monkeypatched ``run_privacy_preflight`` so the transient-
vs-genuine branch, the retry bound, and both message shapes are exercised
with real assertions rather than left to code-review confidence alone.
"""

from __future__ import annotations

import pytest

import conftest as conftest_module
from agenttalk.comprehension.privacy import VcsPrivacyRefused


def _refused(detail: str) -> VcsPrivacyRefused:
    return VcsPrivacyRefused(detail, vcs_kind="git")


def _flaky_then_ok(detail: str, *, fail_calls: int):
    """A fake ``run_privacy_preflight`` that raises ``detail`` for the
    first ``fail_calls`` calls, then returns a sentinel."""
    sentinel = object()
    calls = {"n": 0}

    def fake(root):
        calls["n"] += 1
        if calls["n"] <= fail_calls:
            raise _refused(detail)
        return sentinel

    return fake, calls, sentinel


def _always_refuses(detail: str):
    calls = {"n": 0}

    def fake(root):
        calls["n"] += 1
        raise _refused(detail)

    return fake, calls


# ------------------------------------------------ "could not be trusted" (fix round 1, #223)

def test_transient_could_not_be_trusted_refusal_recovers(monkeypatch) -> None:
    fake, calls, sentinel = _flaky_then_ok(
        "git check-ignore could not be trusted to answer whether "
        ".agenttalk/comprehension/ is ignored",
        fail_calls=2,
    )
    monkeypatch.setattr(conftest_module, "run_privacy_preflight", fake)
    monkeypatch.setattr(conftest_module.time, "sleep", lambda _seconds: None)

    result = conftest_module._run_privacy_preflight_with_bounded_retry(
        "fake-root", attempts=4, delay=0)

    assert result is sentinel
    assert calls["n"] == 3


def test_persistent_could_not_be_trusted_refusal_raises_after_the_bound(monkeypatch) -> None:
    fake, calls = _always_refuses(
        "git ls-files could not be trusted to answer whether "
        ".agenttalk/comprehension/ is tracked"
    )
    monkeypatch.setattr(conftest_module, "run_privacy_preflight", fake)
    monkeypatch.setattr(conftest_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(VcsPrivacyRefused):
        conftest_module._run_privacy_preflight_with_bounded_retry(
            "fake-root", attempts=4, delay=0)
    assert calls["n"] == 4, "bounded - never retried forever"


# --------------------------------------- "is not inside a Git worktree" (fix round 2, PR #228)

def test_transient_not_inside_a_git_worktree_refusal_recovers(monkeypatch) -> None:
    """PR #228: the FIRST git call (rev-parse) timing out produces this
    DIFFERENT message - must be retried too, not just the "could not be
    trusted" shape."""
    fake, calls, sentinel = _flaky_then_ok(
        "'fake-root' is not inside a Git worktree (or git is unavailable) — "
        "ignore status cannot be proven for any supported VCS",
        fail_calls=2,
    )
    monkeypatch.setattr(conftest_module, "run_privacy_preflight", fake)
    monkeypatch.setattr(conftest_module.time, "sleep", lambda _seconds: None)

    result = conftest_module._run_privacy_preflight_with_bounded_retry(
        "fake-root", attempts=4, delay=0)

    assert result is sentinel
    assert calls["n"] == 3


def test_persistent_not_inside_a_git_worktree_refusal_raises_after_the_bound(monkeypatch) -> None:
    fake, calls = _always_refuses(
        "'fake-root' is not inside a Git worktree (or git is unavailable) — "
        "ignore status cannot be proven for any supported VCS"
    )
    monkeypatch.setattr(conftest_module, "run_privacy_preflight", fake)
    monkeypatch.setattr(conftest_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(VcsPrivacyRefused):
        conftest_module._run_privacy_preflight_with_bounded_retry(
            "fake-root", attempts=4, delay=0)
    assert calls["n"] == 4, "bounded - never retried forever"


# --------------------------------------------------------------- genuine refusals: never retried

def test_not_ignored_refusal_raises_on_the_first_call(monkeypatch) -> None:
    """A genuine refusal - real content is stageable/not ignored - is a
    test-setup bug, never transient, and must not be retried into
    silence."""
    fake, calls = _always_refuses(".agenttalk/comprehension/ is not proven ignored by Git")
    monkeypatch.setattr(conftest_module, "run_privacy_preflight", fake)
    monkeypatch.setattr(conftest_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(VcsPrivacyRefused):
        conftest_module._run_privacy_preflight_with_bounded_retry(
            "fake-root", attempts=4, delay=0)
    assert calls["n"] == 1, "a genuine refusal is never retried"


def test_already_tracked_refusal_raises_on_the_first_call(monkeypatch) -> None:
    fake, calls = _always_refuses(
        "1 path(s) under .agenttalk/comprehension/ are already tracked by Git "
        "(e.g. '.agenttalk/comprehension/index.json')"
    )
    monkeypatch.setattr(conftest_module, "run_privacy_preflight", fake)
    monkeypatch.setattr(conftest_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(VcsPrivacyRefused):
        conftest_module._run_privacy_preflight_with_bounded_retry(
            "fake-root", attempts=4, delay=0)
    assert calls["n"] == 1, "a genuine refusal is never retried"
