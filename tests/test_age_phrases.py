"""#615: an age is written once. "after 2d ago", "pending past 10min ago" and "seen just now ago" came from
putting a complete age phrase ("2d ago", "just now") inside a sentence that already supplied its own words."""

from dataclasses import replace

import pytest

from agenttalk import cli, overview
from agenttalk.store import Store


def _command(root, *args):
    return cli.main(["--root", str(root), *args])


# ------------------------------------------------------------ the two pieces of an age


@pytest.mark.parametrize(
    "seconds,span", [(5, "5s"), (59.9, "59s"), (60, "1min"), (600, "10min"), (7200, "2h"), (172800, "2d")]
)
def test_a_length_of_time_is_a_bare_span_and_the_phrase_adds_ago_once(seconds, span):
    assert cli._age_span(seconds) == span
    assert cli._format_age(seconds) == f"{span} ago"
    assert cli._format_age(seconds).count("ago") == 1


# ------------------------------------------------------------ cli: each sentence that composes an age


def _old(store):
    return [replace(message, ts="2000-01-01T00:00:00Z") for message in store.valid_messages()]


def test_the_unanswered_request_warning_names_the_wait_without_ago(tmp_path, capsys):
    assert _command(tmp_path, "init", "--agents", "worker,first") == 0
    assert (
        _command(
            tmp_path,
            "send",
            "--from",
            "worker",
            "--to",
            "first",
            "--kind",
            "question",
            "-m",
            "Which one?",
            "--meta",
            "request_id=q-old",
        )
        == 0
    )
    capsys.readouterr()
    store = Store(tmp_path)
    warnings = cli._thread_warnings(store, store.load_config(), valid_msgs=_old(store))
    text = next(w for w in warnings if "still awaiting" in w)
    assert "ago" not in text
    assert " after " in text and text.split(" after ")[1].split(" ")[0].endswith("d")


def test_the_escalation_warning_names_the_limit_without_ago(tmp_path, capsys):
    assert _command(tmp_path, "init", "--agents", "worker,first,second") == 0
    assert _command(tmp_path, "roster", "set-operator-facing", "second") == 0
    assert (
        _command(
            tmp_path,
            "escalate",
            "--from",
            "worker",
            "--to",
            "first",
            "-m",
            "Choose an option",
            "--meta",
            "request_id=esc-old",
        )
        == 0
    )
    capsys.readouterr()
    store = Store(tmp_path)
    warnings = cli._thread_warnings(store, store.load_config(), valid_msgs=_old(store))
    text = next(w for w in warnings if "operator escalation(s) pending" in w)
    assert "ago" not in text
    assert "pending past 10min [" in text


def test_the_lists_that_label_an_age_still_say_ago_once(tmp_path, capsys):
    assert _command(tmp_path, "init", "--agents", "worker,first") == 0
    assert (
        _command(tmp_path, "send", "--from", "worker", "--to", "first", "--kind", "question", "-m", "Which one?") == 0
    )
    capsys.readouterr()
    assert _command(tmp_path, "threads", "--for", "worker") == 0
    row = next(line for line in capsys.readouterr().out.splitlines() if "age=" in line)
    assert row.count("ago") == 1 and "ago ago" not in row


# ------------------------------------------------------------ the plain-words overview


@pytest.mark.parametrize(
    "seconds,words",
    [
        (10, "seen just now"),
        (89, "seen just now"),
        (300, "seen 5 min ago"),
        (7200, "seen 2 h ago"),
        (172800, "seen 2 d ago"),
        (-1, "seen at an unknown time"),
    ],
)
def test_the_overview_says_when_a_seat_was_seen_without_a_second_ago(seconds, words):
    line = overview._seat_line({"name": "a", "last_seen_age_seconds": seconds})
    assert words in line
    assert "just now ago" not in line and "age ago" not in line and "time ago" not in line


def test_the_overview_item_and_thread_ages_read_as_before():
    data = {
        "state": {
            "label": "proj",
            "counts": {"open_threads": 1},
            "agents": [],
            "threads": [{"request_id": "q-1", "subject": "Fix it", "next_owner": "b", "age_seconds": 10}],
        },
        "attention": {
            "count": 1,
            "items": [
                {"severity": "high", "title": "Reply owed", "agent": "b", "age_seconds": 10, "age_unknown": False}
            ],
        },
        "gates": {"verdict": "OK", "gates": []},
    }
    text = " ".join(overview.render(data, width=400).split())
    assert "Fix it: next move is b's, open just now" in text
    assert "[high] Reply owed (b), just now" in text
