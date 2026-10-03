"""The golden normaliser writes the same text on every platform.

A path below the temporary project folder is written with "/" whether the platform joined
its parts with "\\" (Windows, and "\\\\" inside JSON text) or "/" (POSIX); a JSON escape
right after a path is not swallowed; a platform-dependent byte count is a placeholder.
"""

from __future__ import annotations

import golden_stop_retries_scenarios as g


def fix(text, root, *, json_text=False):
    return g.normalise(text, root, [], json_text=json_text)


class FakeRoot:
    """A root that renders as a Windows path (str) without needing Windows."""

    def __init__(self, text):
        self._text = text

    def __str__(self):
        return self._text


def test_windows_json_escaped_and_posix_paths_normalise_to_the_same_text():
    expected = '"payload_path": "<ROOT>/.agenttalk/dead-letter/beta/m1.json"'
    windows = FakeRoot(r"C:\t\case0")
    assert fix(r'"payload_path": "C:\\t\\case0\\.agenttalk\\dead-letter\\beta\\m1.json"', windows,
               json_text=True) == expected
    assert fix(r'"payload_path": "C:\t\case0\.agenttalk\dead-letter\beta\m1.json"', windows) == expected
    posix = FakeRoot("/work/t/case0")
    assert fix('"payload_path": "/work/t/case0/.agenttalk/dead-letter/beta/m1.json"', posix,
               json_text=True) == expected


def test_a_json_escape_right_after_a_path_is_not_swallowed():
    windows = FakeRoot(r"C:\t\case0")
    text = r'"a": "C:\\t\\case0\\.agenttalk\\x.json\nnext line", "b": "C:\\t\\case0\\nested\\y"'
    assert fix(text, windows, json_text=True) == (
        r'"a": "<ROOT>/.agenttalk/x.json\nnext line", "b": "<ROOT>/nested/y"')


def test_the_root_alone_and_text_that_is_not_a_path_are_left_alone():
    windows = FakeRoot(r"C:\t\case0")
    assert fix("root is C:\\t\\case0.", windows) == "root is <ROOT>."
    assert fix("no paths here: 3\\4", windows) == "no paths here: 3\\4"


def test_a_stored_byte_count_is_a_placeholder_because_windows_line_ends_change_it():
    assert fix('"size_bytes": 193,', FakeRoot("/x")) == '"size_bytes": 0,'
    assert fix('{\n  "size_bytes": 184\n}', FakeRoot("/x")) == '{\n  "size_bytes": 0\n}'


def test_the_golden_file_has_no_backslash_paths_and_no_carriage_returns():
    from pathlib import Path

    raw = (Path(__file__).parent / "golden" / "stop_retries_off_fc791ece.json").read_bytes()
    assert b"\r" not in raw
    assert b"<ROOT>\\" not in raw
