"""#712: every CI download goes through curl's own retry for transient trouble, and nothing else changed.

The pinned gitleaks download used to fail the whole job on one dropped connection or a 503 from the download
host. The fix is four flags on each `curl` in a workflow: `--retry 3 --retry-delay 5 --retry-max-time 120
--connect-timeout 10`. They are deliberately WITHOUT `--retry-all-errors`: curl then retries only timeouts and
HTTP 408, 429, 500, 502, 503 and 504, and stops on everything else (a 404 or a bad certificate is a real
problem, not a blip). The checksum check and the extraction stay exactly where they were, after the download.

This reads the workflow files as text; it needs no network and no curl.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
RETRY_FLAGS = {"--retry": "3", "--retry-delay": "5", "--retry-max-time": "120", "--connect-timeout": "10"}
KEPT_FLAGS = ("--fail", "--location", "--silent", "--show-error")
FORBIDDEN = ("--retry-all-errors",)


def curl_commands(text: str) -> list[tuple[int, list[str]]]:
    """Every `curl` command in a workflow: (line number, its words), with `\\` continuation lines joined."""
    lines, found, number = text.splitlines(), [], 0
    while number < len(lines):
        if re.search(r"(^|[\s;&|(])curl(\s|$)", lines[number]):
            start, joined = number + 1, lines[number]
            while joined.rstrip().endswith("\\") and number + 1 < len(lines):
                number += 1
                joined = joined.rstrip()[:-1] + " " + lines[number]
            found.append((start, joined.split()))
        number += 1
    return found


def problems(words: list[str]) -> list[str]:
    out = [f"missing {flag} {value}" for flag, value in RETRY_FLAGS.items() if _value_of(words, flag) != value]
    out += [f"missing {flag}" for flag in KEPT_FLAGS if flag not in words]
    out += [f"{flag} must not be used" for flag in FORBIDDEN if flag in words]
    return out


def _value_of(words: list[str], flag: str) -> str | None:
    return words[words.index(flag) + 1] if flag in words and words.index(flag) + 1 < len(words) else None


def _step_of(text: str, line_number: int) -> str:
    """The workflow step (from its `- name:` line to the next one) that holds a line."""
    before = text.splitlines()[:line_number]
    first = max(i for i, line in enumerate(before) if re.match(r"\s*- name:", line))
    rest = text.splitlines()[first + 1 :]
    last = next((i for i, line in enumerate(rest) if re.match(r"\s*- name:", line)), len(rest))
    return "\n".join(text.splitlines()[first : first + 1 + last])


ALL_CURLS = [
    pytest.param(path.name, number, words, id=f"{path.name}:{number}")
    for path in sorted(WORKFLOWS.glob("*.yml"))
    for number, words in curl_commands(path.read_text(encoding="utf-8"))
]


def test_the_scan_finds_the_two_gitleaks_downloads() -> None:
    """A scan that finds nothing would pass every check below, so it must find what is known to be there."""
    assert sorted(p.values[0] for p in ALL_CURLS) == ["tests.yml", "tests.yml"]
    urls = [word for p in ALL_CURLS for word in p.values[2] if word.startswith("https://github.com/gitleaks/")]
    assert len(urls) == 2 and urls[0] == urls[1]  # the same pinned archive in both steps


@pytest.mark.parametrize(("workflow", "number", "words"), ALL_CURLS)
def test_every_download_retries_transient_trouble_and_never_retries_everything(workflow, number, words) -> None:
    assert problems(words) == [], f"{workflow}:{number}: {' '.join(words)}"


@pytest.mark.parametrize(("workflow", "number", "words"), ALL_CURLS)
def test_the_checksum_check_and_the_extraction_stay_after_the_download(workflow, number, words) -> None:
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    step = _step_of(text, number)
    assert step.index("curl") < step.index("sha256sum --check -") < step.index("tar -xzf"), step
    assert step.count("curl") == 1  # one download per step, so a retry never re-runs the check or the extraction


# --------------------------------------------------------------------- the scanner itself, on invented text

GOOD = """\
      - name: Install
        run: |
          curl --fail --location --silent --show-error \\
            --retry 3 --retry-delay 5 --retry-max-time 120 --connect-timeout 10 \\
            https://example.invalid/a.tar.gz --output "$archive"
"""


def test_the_scanner_reads_a_continued_command_and_accepts_the_pinned_flags() -> None:
    ((number, words),) = curl_commands(GOOD)
    assert number == 3 and problems(words) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (("--retry 3", ""), "missing --retry 3"),
        (("--retry 3", "--retry 4"), "missing --retry 3"),
        (("--retry-delay 5", "--retry-delay 1"), "missing --retry-delay 5"),
        (("--retry-max-time 120", "--retry-max-time 600"), "missing --retry-max-time 120"),
        (("--connect-timeout 10", ""), "missing --connect-timeout 10"),
        (("--fail", ""), "missing --fail"),
        (("--retry 3", "--retry 3 --retry-all-errors"), "--retry-all-errors must not be used"),
    ],
)
def test_the_scanner_names_each_way_a_download_can_go_wrong(change, expected) -> None:
    old, new = change
    ((_, words),) = curl_commands(GOOD.replace(old, new, 1))
    assert expected in problems(words)


def test_a_curl_in_a_comment_or_a_word_is_not_a_command() -> None:
    assert curl_commands("# nothing here\n  echo securecurl-is-not-curl\n") == []
