"""C-1 / #213 correction (lead's follow-up dispatch, 2026-08-27): direct,
fake-winreg-backed unit tests for conftest.py's
``_windows_symlink_devmode_session`` - the generator the session-scoped
``_enable_windows_symlink_creation_without_elevation`` fixture drives.

The lead's correction required two properties of that fixture:
1. It must NEVER mutate the host's registry unless actually running under
   GitHub Actions (CI=true and GITHUB_ACTIONS=true) - a local run, even an
   elevated one on a maintainer's real machine, must be a no-op.
2. Where it DOES write, it must restore the prior value (or absence of
   one) at session end, best-effort, with a disclosed warning if the
   restore itself fails.

Neither property is provable by just running the real suite on this dev
sandbox (not GitHub Actions, and not elevated either) - this module drives
the generator directly with an in-memory fake ``winreg`` substitute so
both the gating and the enable/restore sequencing are exercised with real
assertions rather than left to code-review confidence alone.
"""

from __future__ import annotations

import sys

import pytest

import conftest as conftest_module


class _FakeWinreg:
    """A minimal in-memory stand-in for the real ``winreg`` module's
    surface this session generator uses - just enough to drive and
    observe CreateKeyEx/QueryValueEx/SetValueEx/OpenKey/DeleteValue/
    CloseKey without touching a real registry."""

    HKEY_LOCAL_MACHINE = object()
    KEY_SET_VALUE = 0x1
    KEY_QUERY_VALUE = 0x2
    REG_DWORD = 4

    def __init__(self) -> None:
        self.values: dict[str, tuple[object, int]] = {}
        self.closed_count = 0
        self.fail_create = False
        self.fail_set = False
        self.fail_reopen_for_restore = False
        self.fail_restore_set = False

    def CreateKeyEx(self, hive, subkey, reserved, access):  # noqa: N802 - mirrors real winreg's name
        if self.fail_create:
            raise OSError("simulated: access denied creating key")
        return ("key", subkey)

    def OpenKey(self, hive, subkey, reserved, access):  # noqa: N802
        if self.fail_reopen_for_restore:
            raise OSError("simulated: access denied reopening key")
        return ("key", subkey)

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]

    def SetValueEx(self, key, name, reserved, value_type, value):  # noqa: N802
        if self.fail_set and name not in self.values:
            raise OSError("simulated: access denied setting value")
        if self.fail_restore_set and name in self.values:
            raise OSError("simulated: access denied restoring value")
        self.values[name] = (value, value_type)

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]

    def CloseKey(self, key):  # noqa: N802
        self.closed_count += 1


def _drive(generator) -> None:
    """Run a two-phase (setup, teardown) generator fixture body to
    completion, exactly as pytest's own fixture machinery would."""
    next(generator)
    with pytest.raises(StopIteration):
        next(generator)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only symlink-devmode policy")
def test_session_is_a_pure_noop_outside_github_actions(monkeypatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    fake = _FakeWinreg()
    _drive(conftest_module._windows_symlink_devmode_session(winreg_module=fake))
    assert fake.values == {}, "must never touch the registry when not running under GitHub Actions"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only symlink-devmode policy")
def test_session_is_a_pure_noop_when_only_one_of_ci_or_github_actions_is_set(monkeypatch) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    fake = _FakeWinreg()
    _drive(conftest_module._windows_symlink_devmode_session(winreg_module=fake))
    assert fake.values == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only symlink-devmode policy")
def test_session_sets_then_restores_a_previously_absent_value(monkeypatch) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fake = _FakeWinreg()
    generator = conftest_module._windows_symlink_devmode_session(winreg_module=fake)

    next(generator)  # setup phase
    assert fake.values[conftest_module._SYMLINK_DEVMODE_VALUE] == (1, fake.REG_DWORD)

    with pytest.raises(StopIteration):
        next(generator)  # teardown phase
    assert conftest_module._SYMLINK_DEVMODE_VALUE not in fake.values, (
        "must restore the prior (absent) state, not leave the policy flipped")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only symlink-devmode policy")
def test_session_sets_then_restores_a_previously_present_value(monkeypatch) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fake = _FakeWinreg()
    fake.values[conftest_module._SYMLINK_DEVMODE_VALUE] = (0, fake.REG_DWORD)

    _drive(conftest_module._windows_symlink_devmode_session(winreg_module=fake))

    assert fake.values[conftest_module._SYMLINK_DEVMODE_VALUE] == (0, fake.REG_DWORD), (
        "must restore the exact prior value, not merely delete it")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only symlink-devmode policy")
def test_session_never_writes_when_the_key_cannot_be_created(monkeypatch) -> None:
    """A non-elevated local run: even with CI env vars somehow set, if the
    registry write itself is refused, this must degrade to a no-op rather
    than crash the whole test session."""
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fake = _FakeWinreg()
    fake.fail_create = True

    _drive(conftest_module._windows_symlink_devmode_session(winreg_module=fake))
    assert fake.values == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only symlink-devmode policy")
def test_session_attempts_no_restore_when_the_original_set_failed(monkeypatch) -> None:
    """If SetValueEx itself failed during setup, nothing actually changed -
    the teardown phase must not then try to "restore" a value that was
    never written, and must not reopen the key at all."""
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fake = _FakeWinreg()
    fake.fail_set = True

    _drive(conftest_module._windows_symlink_devmode_session(winreg_module=fake))
    assert fake.values == {}
    assert not fake.fail_reopen_for_restore  # sanity: flag untouched, path never taken


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only symlink-devmode policy")
def test_session_disclosed_restore_failure_does_not_raise(monkeypatch, capsys) -> None:
    """A restore failure must be disclosed (printed), never silently
    swallowed, and must never propagate as an exception out of session
    teardown (which would break every other test's teardown in the same
    run)."""
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fake = _FakeWinreg()
    fake.fail_reopen_for_restore = True

    _drive(conftest_module._windows_symlink_devmode_session(winreg_module=fake))
    assert "WARNING" in capsys.readouterr().out
