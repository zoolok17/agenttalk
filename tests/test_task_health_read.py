"""Task compatibility checks across temporary and persistent health read failures."""

import json

import pytest

from agenttalk import cli
from agenttalk.store import Store


@pytest.fixture
def busy_seat(store, monkeypatch):
    store.set_role("alpha", "lead")
    store.write_health("beta", {"state": "running", "agenttalk_version": "0.88.0"})
    pauses = []
    monkeypatch.setattr(cli.time, "sleep", pauses.append)
    return pauses


def dispatch(root, *, force=False, broadcast=False):
    argv = (["broadcast", "--all", "--kind", "task"] if broadcast
            else ["task", "--to", "beta"])
    return cli.main(["--root", str(root), *argv, "--from", "alpha", "-m", "work",
                     *(["--force"] if force else [])])


def fail_reads(store, monkeypatch, failures):
    """Wrap the store-owned path boundary; unrelated reads use real files."""
    original = Store.health_path
    target = original(store, "beta")
    attempts = []

    class HealthPath:
        def exists(self):
            return target.exists()

        def read_text(self, **kwargs):
            attempts.append(None)
            if failures is None or len(attempts) <= failures:
                raise PermissionError("synthetic private path must not be printed")
            return target.read_text(**kwargs)

    monkeypatch.setattr(Store, "health_path",
                        lambda self, agent: HealthPath() if agent == "beta" else original(self, agent))
    return attempts


@pytest.mark.parametrize("broadcast", [False, True])
@pytest.mark.parametrize("failures", [1, 2])
def test_current_busy_seat_recovers_from_transient_read_errors(
    store, store_root, busy_seat, monkeypatch, capsys, broadcast, failures,
):
    attempts = fail_reads(store, monkeypatch, failures)
    assert dispatch(store_root, broadcast=broadcast) == 0
    assert [m.kind for m in store.messages_for("beta")] == ["task"]
    assert len(attempts) == failures + 1
    assert "predates" not in capsys.readouterr().err


def test_current_busy_seat_recovers_from_empty_file_during_rewrite(
    store, store_root, busy_seat, monkeypatch,
):
    path = store.health_path("beta")
    saved = path.read_text(encoding="utf-8")
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(cli.time, "sleep", lambda delay: path.write_text(saved, encoding="utf-8"))
    assert dispatch(store_root) == 0
    assert len(store.messages_for("beta")) == 1


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("broadcast", [False, True])
def test_unreadable_health_reports_uncertainty_and_bounds_retries(
    store, store_root, busy_seat, monkeypatch, capsys, force, broadcast,
):
    attempts = fail_reads(store, monkeypatch, None)
    assert dispatch(store_root, force=force, broadcast=broadcast) == (0 if force else 2)
    err = capsys.readouterr().err
    assert "could not read its health record" in err
    assert "PermissionError" in err
    assert "try again" in err
    assert "predates" not in err and "NOT see" not in err and "unsupported by" not in err
    assert "private path" not in err
    assert len(attempts) == 3
    assert len(busy_seat) == 2 and 0 < sum(busy_seat) <= 0.1
    assert len(store.messages_for("beta")) == int(force)
    if force:
        assert "not known whether" in err


@pytest.mark.parametrize("raw, reason", [
    ("", "JSONDecodeError"), ("{", "JSONDecodeError"), ("[]", "ValueError"),
    ("{}", "no version"), ('{"agenttalk_version": null}', "no version"),
    ('{"agenttalk_version": " "}', "no version"), ('{"agenttalk_version": 88}', "no version"),
])
@pytest.mark.parametrize("force", [False, True])
def test_bad_or_versionless_record_is_not_called_an_old_build(
    store, store_root, busy_seat, capsys, raw, reason, force,
):
    store.health_path("beta").write_text(raw, encoding="utf-8")
    assert dispatch(store_root, force=force) == (0 if force else 2)
    err = capsys.readouterr().err
    assert reason in err
    assert "predates" not in err and "NOT see" not in err
    assert len(store.messages_for("beta")) == int(force)


@pytest.mark.parametrize("force", [False, True])
def test_old_version_after_transient_read_error_still_has_real_old_build_warning(
    store, store_root, busy_seat, monkeypatch, capsys, force,
):
    store.write_health("beta", {"agenttalk_version": "0.87.3"})
    fail_reads(store, monkeypatch, 1)
    assert dispatch(store_root, force=force) == (0 if force else 2)
    err = capsys.readouterr().err
    assert "beta (0.87.3)" in err and "predates task-kind support" in err
    assert len(store.messages_for("beta")) == int(force)


def test_shared_advisory_reader_still_returns_none_for_bad_record(store, busy_seat):
    store.health_path("beta").write_text("{", encoding="utf-8")
    assert store.read_health_raw("beta") is None
    store.health_path("beta").write_text(json.dumps({"state": "running"}), encoding="utf-8")
    assert store.read_health_raw("beta") == {"state": "running"}


@pytest.fixture
def absent_health(store, monkeypatch):
    store.set_role("alpha", "lead")
    path = store.health_path("beta")
    original = Store.health_path
    reads, pauses = [], []

    class MissingHealthPath:
        def exists(self):
            return path.exists()

        def read_text(self, **kwargs):
            reads.append(None)
            return path.read_text(**kwargs)

    monkeypatch.setattr(Store, "health_path",
                        lambda self, agent: MissingHealthPath() if agent == "beta" else original(self, agent))
    monkeypatch.setattr(cli.time, "sleep", pauses.append)
    return reads, pauses


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("broadcast", [False, True])
def test_never_started_recipient_is_named_without_missing_file_retries(
    store, store_root, absent_health, capsys, force, broadcast,
):
    assert dispatch(store_root, force=force, broadcast=broadcast) == (0 if force else 2)
    err = capsys.readouterr().err
    reads, pauses = absent_health
    assert len(reads) == 1 and pauses == [], f"reads={len(reads)}, pauses={pauses}, stderr={err}"
    assert "has not started yet" in err and "no heartbeat and no health record" in err
    assert "could not read" not in err and "try again" not in err and "predates" not in err
    assert len(store.messages_for("beta")) == int(force)


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("broadcast", [False, True])
def test_heartbeat_without_health_record_reports_absence_without_retries(
    store, store_root, absent_health, capsys, force, broadcast,
):
    store.write_heartbeat("beta")
    assert dispatch(store_root, force=force, broadcast=broadcast) == (0 if force else 2)
    err = capsys.readouterr().err
    reads, pauses = absent_health
    assert len(reads) == 1 and pauses == [], f"reads={len(reads)}, pauses={pauses}, stderr={err}"
    assert "no health record" in err
    assert "has not started" not in err and "could not read" not in err and "try again" not in err
    assert "predates" not in err
    assert len(store.messages_for("beta")) == int(force)
