"""The golden normaliser makes the same text on every platform and release, and never hides a
broken value.

Stored JSON is decoded and compared value by value (paths, release number and byte counts
are normalised on the DECODED values, then written back in one fixed form); JSON text is never
rewritten for paths, because there a backslash can also start an escape. Plain text keeps the
root and backslash handling.
"""

from __future__ import annotations

import json

import golden_stop_retries_scenarios as g


class FakeRoot:
    """A root that renders as a given path (str) without needing that platform."""

    def __init__(self, text):
        self._text = text

    def __str__(self):
        return self._text


WINDOWS = FakeRoot(r"C:\t\case0")
POSIX = FakeRoot("/work/t/case0")
EXPECTED_PATH = "<ROOT>/.agenttalk/dead-letter/beta/m1.json"


def stored(path, **more):
    return json.dumps({"payload_path": path, "size_bytes": 193, **more})


def test_windows_and_posix_paths_read_the_same_in_plain_text():
    assert g.normalise(r"C:\t\case0\.agenttalk\dead-letter\beta\m1.json", WINDOWS, []) == EXPECTED_PATH
    assert g.normalise("/work/t/case0/.agenttalk/dead-letter/beta/m1.json", POSIX, []) == EXPECTED_PATH
    assert g.normalise("root is C:\\t\\case0.", WINDOWS, []) == "root is <ROOT>."
    assert g.normalise("no paths here: 3\\4", WINDOWS, []) == "no paths here: 3\\4"


def test_windows_and_posix_paths_read_the_same_in_stored_json():
    windows = g.normalise_file(stored(r"C:\t\case0\.agenttalk\dead-letter\beta\m1.json"), WINDOWS, [])
    posix = g.normalise_file(stored("/work/t/case0/.agenttalk/dead-letter/beta/m1.json"), POSIX, [])
    assert windows == posix
    assert json.loads(windows)["payload_path"] == EXPECTED_PATH


def test_a_control_character_in_a_stored_path_is_not_hidden():
    good = stored(r"C:\t\case0\.agenttalk\dead-letter\beta\m1.json")
    # the separator before "beta" became a backspace escape: "\b" + "eta" in the JSON text
    bad = good.replace(r"dead-letter\\beta", r"dead-letter\beta")
    assert "\\beta" in bad and bad != good
    assert json.loads(bad)["payload_path"].count("\b") == 1
    assert g.normalise_file(bad, WINDOWS, []) != g.normalise_file(good, WINDOWS, [])
    assert "\\b" in g.normalise_file(bad, WINDOWS, [])


def test_a_changed_file_name_below_the_root_stays_visible():
    first = g.normalise_file(stored("/work/t/case0/.agenttalk/dead-letter/beta/first.json"), POSIX, [])
    second = g.normalise_file(stored("/work/t/case0/.agenttalk/dead-letter/beta/second.json"), POSIX, [])
    assert first != second


def test_a_json_escape_right_after_a_path_is_kept():
    text = json.dumps({"a": "/work/t/case0/.agenttalk/x.json\nnext line", "b": "/work/t/case0/nested/y"})
    out = json.loads(g.normalise_file(text, POSIX, []))
    assert out == {"a": "<ROOT>/.agenttalk/x.json\nnext line", "b": "<ROOT>/nested/y"}


def test_the_release_number_and_the_byte_count_are_placeholders_and_nothing_else_is():
    one = g.normalise_file(json.dumps({"agenttalk_version": "0.95.0", "size_bytes": 193, "reason_code": "a"}),
                           POSIX, [])
    two = g.normalise_file(json.dumps({"agenttalk_version": "99.1.2", "size_bytes": 184, "reason_code": "a"}),
                           POSIX, [])
    other = g.normalise_file(json.dumps({"agenttalk_version": "99.1.2", "size_bytes": 184, "reason_code": "b"}),
                             POSIX, [])
    assert one == two and one != other
    assert json.loads(one) == {"agenttalk_version": "<VERSION>", "size_bytes": 0, "reason_code": "a"}


def test_a_non_integer_size_or_non_string_version_is_not_hidden():
    out = json.loads(g.normalise_file(json.dumps({"agenttalk_version": 5, "size_bytes": "193"}), POSIX, []))
    assert out == {"agenttalk_version": 5, "size_bytes": "193"}


def test_a_file_of_json_lines_is_decoded_line_by_line():
    text = "\n".join(json.dumps({"path": "/work/t/case0/a.json", "agenttalk_version": "1"}) for _ in range(2))
    lines = g.normalise_file(text, POSIX, []).splitlines()
    assert [json.loads(line) for line in lines] == [{"path": "<ROOT>/a.json", "agenttalk_version": "<VERSION>"}] * 2


def test_text_that_is_not_json_is_plain_text():
    assert g.normalise_file("log line at /work/t/case0/x.log\n", POSIX, []) == "log line at <ROOT>/x.log\n"
    assert g.normalise_file("", POSIX, []) == ""
    assert g.normalise_file("[1, 2", POSIX, []) == "[1, 2"


def test_the_golden_file_has_no_backslash_paths_no_release_number_and_no_carriage_returns():
    from pathlib import Path

    raw = (Path(__file__).parent / "golden" / "stop_retries_off_fc791ece.json").read_bytes()
    assert b"\r" not in raw and b"<ROOT>\\" not in raw
    assert b"0.95" not in raw and b"<VERSION>" in raw
