"""#596: `capacity show` keeps an older reading, shown with its age and a stale label, never as current."""

import io
from contextlib import redirect_stdout
from datetime import timedelta

from agenttalk import capacity as cap, cli
from agenttalk.wrapper import session as wsession
from capacity_class_helpers import AT, isolate

ACCOUNT = "claude:synthetic:home-00000000"
OLD = AT - timedelta(seconds=660)  # just past the 600-second staleness line


def _publish_without_figures(store, *, account=ACCOUNT):
    """What the wrapper writes once the seat's reading is past the staleness line: no figures."""
    snap = cap.CapacitySnapshot.unknown("alpha", account=account).to_dict()
    snap.update(source="claude_stream", confidence="stale", reason="claude_stream_stale", observed_at=OLD.isoformat())
    store.write_capacity("alpha", snap)


def _save_weekly_reading(store, *, at, binding=ACCOUNT):
    """The seat's own last allowance reading, as its wrapper keeps it in the session file."""
    reading = cap.claude_stream_reading(
        {
            "rateLimitType": "seven_day",
            "status": "allowed_warning",
            "utilization": 0.91,
            "resetsAt": int(AT.timestamp()) + 3 * 86400,
        },
        {"binding": binding},
        observed_at=at.isoformat(),
    )
    wsession.save_session(store, "alpha", wsession.SessionState(cli="claude", claude_rate_limit=reading))


def _show(store) -> str:
    output = io.StringIO()
    with redirect_stdout(output):
        assert cli.main(["--root", str(store.root), "capacity", "show"]) == 0
    return output.getvalue()


def test_show_keeps_an_older_session_reading_with_its_age(tmp_path, monkeypatch, store):
    isolate(monkeypatch, tmp_path)
    _publish_without_figures(store)
    _save_weekly_reading(store, at=OLD)
    text = _show(store)
    assert "weekly was 91% (seen 11min ago, stale" in text
    assert "91% used" not in text, "an older reading was shown as current"


def test_show_never_uses_a_session_reading_from_another_account(tmp_path, monkeypatch, store):
    isolate(monkeypatch, tmp_path)
    _publish_without_figures(store)
    _save_weekly_reading(store, at=OLD, binding="claude:synthetic:home-11111111")
    text = _show(store)
    assert "91%" not in text
    assert "no current figures" in text


def test_show_presents_a_current_session_reading_as_current(tmp_path, monkeypatch, store):
    isolate(monkeypatch, tmp_path)
    _publish_without_figures(store)
    _save_weekly_reading(store, at=AT - timedelta(seconds=300))
    text = _show(store)
    assert "weekly 91% used, allowed_warning" in text
    assert "stale" not in text


def test_an_older_window_whose_reset_has_passed_says_so(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    snap = cap.CapacitySnapshot.unknown("alpha", account=ACCOUNT).to_dict()
    snap.update(
        source="claude_stream",
        confidence="observed",
        observed_at=AT.isoformat(),
        primary_used_percent=12,
        primary_observed_at=AT.isoformat(),
        secondary_used_percent=91,
        secondary_observed_at=OLD.isoformat(),
        secondary_resets_at=int(AT.timestamp()) - 60,
    )
    text = cli._capacity_text(snap, threshold=80, reset_soon_min=30)
    assert "5h 12% used" in text
    assert "weekly was 91% (seen 11min ago, stale; reset since)" in text
    assert "91% used" not in text


# #599 review: a merged row keeps each part's own time and origin.
FRESH = AT - timedelta(seconds=10)
RECENT = AT - timedelta(seconds=60)


def _publish_statusline(store, *, at, five=None, week=None, context=None):
    """A status-line snapshot as published: one time for every part, no per-part times."""
    snap = cap.CapacitySnapshot.unknown("alpha", account=ACCOUNT).to_dict()
    snap.update(
        source="claude_statusline",
        confidence="observed",
        observed_at=at.isoformat(),
        primary_used_percent=five,
        secondary_used_percent=week,
        context_used_percent=context,
    )
    store.write_capacity("alpha", snap)


def test_a_fresh_saved_window_does_not_make_older_published_figures_current(tmp_path, monkeypatch, store):
    isolate(monkeypatch, tmp_path)
    _publish_statusline(store, at=OLD, five=99, context=97)
    _save_weekly_reading(store, at=FRESH)
    text = _show(store)
    assert "weekly 91% used, allowed_warning" in text
    assert "5h was 99% (seen 11min ago, stale" in text
    assert "context was 97% (seen 11min ago, stale)" in text
    assert "99% used" not in text and "context 97%" not in text, "an old figure was shown as current"
    assert "5h 99%≥" not in text and "context 97%≥" not in text, "an old figure raised a warning"


def test_a_newer_published_window_is_kept_over_an_older_saved_one(tmp_path, monkeypatch, store):
    isolate(monkeypatch, tmp_path)
    _publish_statusline(store, at=RECENT, week=12)
    _save_weekly_reading(store, at=OLD)
    text = _show(store)
    assert "weekly 12% used" in text
    assert "91%" not in text


def test_an_older_published_window_gives_way_to_a_newer_saved_one(tmp_path, monkeypatch, store):
    isolate(monkeypatch, tmp_path)
    _publish_statusline(store, at=OLD, week=12)
    _save_weekly_reading(store, at=FRESH)
    text = _show(store)
    assert "weekly 91% used, allowed_warning" in text
    assert "12%" not in text


def test_a_window_from_the_saved_reading_names_its_source(tmp_path, monkeypatch, store):
    isolate(monkeypatch, tmp_path)
    _publish_statusline(store, at=RECENT, five=30)
    _save_weekly_reading(store, at=FRESH)
    text = _show(store)
    assert "5h 30% used" in text and "weekly 91% used" in text
    assert "source=claude_statusline, weekly from claude_stream" in text
