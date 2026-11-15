"""#712: every CI download goes through curl's own retry for transient trouble, and nothing else changed.

The pinned gitleaks download used to fail the whole job on one dropped connection or a 503 from the download
host. The fix is four flags on each `curl` in a workflow: `--retry 3 --retry-delay 5 --retry-max-time 120
--connect-timeout 10`. They are deliberately WITHOUT `--retry-all-errors`: curl then retries only its default
transient failures (timeouts and the HTTP statuses curl documents, such as 408, 429, 500, 502, 503 and 504) and
stops on everything else (a 404 or a bad certificate is a real problem, not a blip). The checksum check and the
extraction stay exactly where they were, after the download.

The test takes each active curl command of a workflow (comments are dropped and backslash continuation lines
are joined) and splits it into words with `shlex`, which removes quotes. So `"--retry" 0` is the option `--retry`
given a second time, and `--retry "3"` is the same as `--retry 3`. curl uses the LAST value of a repeated option,
so a required flag may appear only once: a later `--retry 0` would silently turn the retries off. A `curl` inside
a shell comment is not a command. GitHub runs both `*.yml` and `*.yaml` files, so both are read.

Known limits. The self-tests copy the live file's spelling, so a harmless rewrite of the real downloads (a quoted
value, reordered flags, folded lines, a comment after the checksum line) can fail them. Text inside a heredoc is
counted as a command. A quoted `#` confuses the comment stripping. Only `curl` downloads are checked, not `wget`.
The loop pins and the fake-curl run copy the loop's own spelling, and the run needs bash (Git Bash, Linux or macOS).

A download that curl's own retry does not cover is retried by a small loop around it: curl exit 18 (the transfer
broke off part way) and 56 (a receive failure) are retried up to three more times, after removing the partial file,
and every other exit code ends the step at once (a 404 is 22, a bad certificate is 60; timeouts, 28, are already
retried by `--retry`). The loop is checked as text and also run with a fake `curl` and a fake `sleep` in bash.

This reads the workflow files as text; it needs no network and no curl.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path, PureWindowsPath

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
RETRY_FLAGS = {"--retry": "3", "--retry-delay": "5", "--retry-max-time": "120", "--connect-timeout": "10"}
KEPT_FLAGS = ("--fail", "--location", "--silent", "--show-error")
FORBIDDEN = ("--retry-all-errors",)
CURL = re.compile(r"(^|[\s;&|(])curl(\s|$)")
CHECKSUM = re.compile(r'\s*echo\s+"[0-9a-f]{64}\s+\$archive"\s*\|\s*sha256sum --check -\s*')
EXTRACTION = re.compile(r"\s*tar -xzf\s")


def workflow_files(directory: Path) -> list[Path]:
    """The files GitHub runs as workflows: `*.yml` and `*.yaml`."""
    return sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])


def code_of(line: str) -> str:
    """A shell line without its comment: `#` starts a comment at the start of a word (not inside one, as in a URL)."""
    return re.split(r"(?:^|\s)#", line, maxsplit=1)[0]


def commands_of(text: str) -> list[tuple[int, str]]:
    """The shell commands of a workflow as (first line number, text): comments left out, continuation lines
    joined. A commented-out line is not part of any command, and a backslash at the end of a comment is part of it."""
    lines, found, number = text.splitlines(), [], 0
    while number < len(lines):
        code, start = code_of(lines[number]), number + 1
        if code.strip():
            while code.rstrip().endswith("\\") and number + 1 < len(lines):
                number += 1
                code = code.rstrip()[:-1] + " " + code_of(lines[number])
            found.append((start, code))
        number += 1
    return found


def shell_words(command: str) -> list[str]:
    """The words the shell hands to the program: quotes removed, as `shlex` reads them. A `;` glued to the last word
    (`--output "$archive"; do`) is a word of its own, so a flag added just before it is still seen."""
    try:
        words = shlex.split(command)
    except ValueError:
        return ["<unreadable: unbalanced quote>"]
    out: list[str] = []
    for word in words:
        out += [word[:-1], ";"] if len(word) > 1 and word.endswith(";") else [word]
    return out


def curl_commands(text: str) -> list[tuple[int, list[str]]]:
    """Every `curl` command in a workflow: (line number, its words)."""
    return [(start, shell_words(code)) for start, code in commands_of(text) if CURL.search(code)]


def problems(words: list[str]) -> list[str]:
    out = []
    for flag, value in RETRY_FLAGS.items():
        given = [i for i, word in enumerate(words) if word == flag]
        if len(given) > 1:
            out.append(f"{flag} is given {len(given)} times (curl uses the last)")
        elif not given or words[given[0] + 1 : given[0] + 2] != [value]:
            out.append(f"missing {flag} {value}")
    out += [f"missing {flag}" for flag in KEPT_FLAGS if flag not in words]
    out += [f"{flag} must not be used" for flag in FORBIDDEN if flag in words]
    return out


def _step_of(text: str, line_number: int) -> str:
    """The workflow step (from its `- name:` line to the next one) that holds a line."""
    lines = text.splitlines()
    first = max(i for i, line in enumerate(lines[:line_number]) if re.match(r"\s*- name:", line))
    rest = lines[first + 1 :]
    last = next((i for i, line in enumerate(rest) if re.match(r"\s*- name:", line)), len(rest))
    return "\n".join(lines[first : first + 1 + last])


def step_problems(text: str, line_number: int) -> list[str]:
    """In the step that holds the curl: one download, then an ACTIVE checksum check, then an ACTIVE extraction."""
    commands = [code for _, code in commands_of(_step_of(text, line_number))]
    downloads = [i for i, code in enumerate(commands) if CURL.search(code)]
    checks = [i for i, code in enumerate(commands) if CHECKSUM.fullmatch(code)]
    extractions = [i for i, code in enumerate(commands) if EXTRACTION.match(code)]
    out = []
    if len(downloads) != 1:
        out.append(f"the step has {len(downloads)} downloads (one per step, so a retry never re-runs the check)")
    if not checks:
        out.append("no active checksum check of the archive (echo of the digest piped to sha256sum --check -)")
    if not extractions:
        out.append("no active extraction (tar -xzf)")
    if len(downloads) == 1 and checks and extractions and not downloads[0] < checks[0] < extractions[0]:
        out.append("the download, the checksum check and the extraction are not in that order")
    return out


def violations(name: str, text: str) -> list[str]:
    """Everything wrong with the downloads in one workflow text."""
    out = []
    for number, words in curl_commands(text):
        out += [f"{name}:{number}: {problem}" for problem in problems(words)]
        out += [f"{name}:{number}: {problem}" for problem in step_problems(text, number)]
    return out


def directory_violations(directory: Path) -> list[str]:
    return [
        problem
        for path in workflow_files(directory)
        for problem in violations(path.name, path.read_text(encoding="utf-8"))
    ]


ALL_CURLS = [
    pytest.param(path.name, number, words, id=f"{path.name}:{number}")
    for path in workflow_files(WORKFLOWS)
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
    assert step_problems(text, number) == [], _step_of(text, number)


def test_every_real_workflow_is_clean() -> None:
    assert directory_violations(WORKFLOWS) == []


# ----------------------------------------- the real workflow, changed in the ways a download could be switched off

REAL = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
OUTPUT = '--output "$archive"'
CHECK_ECHO = '          echo "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb  $archive" \\\n'
CHECK_PIPE = "            | sha256sum --check -\n"
EXTRACTION_LINE = '          tar -xzf "$archive" -C "$bindir" gitleaks\n'


def test_the_unchanged_real_workflow_has_no_violations() -> None:
    assert REAL.count(OUTPUT) == 2 and REAL.count(CHECK_ECHO) == 2 and REAL.count(CHECK_PIPE) == 2
    assert violations("tests.yml", REAL) == []


@pytest.mark.parametrize("appended", ["--retry 0", '"--retry" 0', "'--retry' 0", '--retry "0"'])
def test_a_later_retry_override_is_caught_however_it_is_quoted(appended) -> None:
    found = violations("tests.yml", REAL.replace(OUTPUT, f"{OUTPUT} {appended}"))
    assert len(found) == 2 and all("--retry is given 2 times (curl uses the last)" in line for line in found), found


@pytest.mark.parametrize("appended", ["--retry-all-errors", '"--retry-all-errors"', "'--retry-all-errors'"])
def test_retrying_everything_is_caught_however_it_is_quoted(appended) -> None:
    found = violations("tests.yml", REAL.replace(OUTPUT, f"{OUTPUT} {appended}"))
    assert len(found) == 2 and all("--retry-all-errors must not be used" in line for line in found), found


def test_commenting_out_both_real_downloads_leaves_the_scan_with_nothing_to_find() -> None:
    changed = re.sub(
        r"^(\s*)until curl\b", r"\1# until curl", REAL, flags=re.MULTILINE
    )  # whatever order the flags are in
    assert changed != REAL and curl_commands(changed) == []  # the presence test above then fails: nothing runs


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (CHECK_ECHO + CHECK_PIPE, "          # echo the digest | sha256sum --check -\n"),  # both lines commented out
        (CHECK_ECHO, "          # " + CHECK_ECHO.strip() + "\n"),  # the echo commented out, the pipe left
        (CHECK_PIPE, "            # | sha256sum --check -\n"),  # the echo left, the pipe commented out
        (EXTRACTION_LINE, "          # " + EXTRACTION_LINE.strip() + "\n"),  # the extraction commented out
    ],
    ids=["both-check-lines", "echo-only", "pipe-only", "extraction"],
)
def test_a_commented_out_checksum_or_extraction_is_caught(old, new) -> None:
    changed = REAL.replace(old, new)
    found = violations("tests.yml", changed)
    assert changed != REAL and len(found) == 2 and all("no active" in line for line in found), found


def test_an_extraction_moved_before_the_checksum_check_is_caught() -> None:
    moved = REAL.replace(EXTRACTION_LINE, "").replace(CHECK_ECHO, EXTRACTION_LINE + CHECK_ECHO)
    found = violations("tests.yml", moved)
    assert moved != REAL and len(found) == 2 and all("not in that order" in line for line in found), found


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("--retry 3", '--retry "3"'),
        ("--retry 3", "--retry '3'"),
        ("--retry-delay 5", '"--retry-delay" 5'),
        ("curl --fail --location --silent --show-error", "curl --location --fail --silent --show-error"),
        ("curl --fail --location --silent --show-error", "curl --show-error --silent --location --fail"),
        ("--retry 3 --retry-delay 5", "--retry-delay 5 --retry 3"),
    ],
    ids=["double-quoted-3", "single-quoted-3", "quoted-flag", "swap-fail-location", "reverse-kept", "swap-retry-delay"],
)
def test_a_harmless_rewrite_of_the_real_downloads_stays_green(old, new) -> None:
    changed = REAL.replace(old, new)
    assert changed != REAL and violations("tests.yml", changed) == []


def test_an_unretried_curl_in_an_extra_yaml_file_is_caught(tmp_path: Path) -> None:
    copy = tmp_path / "workflows"
    shutil.copytree(WORKFLOWS, copy)
    assert directory_violations(copy) == []
    (copy / "extra.yaml").write_text(
        "jobs:\n  x:\n    steps:\n      - name: Download\n        run: |\n"
        "          curl --fail --location --silent --show-error https://example.invalid/a.tar.gz --output a\n",
        encoding="utf-8",
    )
    found = directory_violations(copy)
    assert found and all(line.startswith("extra.yaml:") for line in found), found


def test_the_census_reads_both_yml_and_yaml_and_nothing_else(tmp_path: Path) -> None:
    for name in ("a.yml", "b.yaml", "c.txt", "d.yml.bak"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert [p.name for p in workflow_files(tmp_path)] == ["a.yml", "b.yaml"]


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
        (("--retry 3", '--retry 3 "--retry-all-errors"'), "--retry-all-errors must not be used"),
    ],
)
def test_the_scanner_names_each_way_a_download_can_go_wrong(change, expected) -> None:
    old, new = change
    ((_, words),) = curl_commands(GOOD.replace(old, new, 1))
    assert expected in problems(words)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (("--retry 3", "--retry 3 --retry 0"), "--retry is given 2 times (curl uses the last)"),
        (("--retry 3", "--retry 0 --retry 3"), "--retry is given 2 times (curl uses the last)"),
        (("--retry 3", '--retry 3 "--retry" 0'), "--retry is given 2 times (curl uses the last)"),
        (("--retry-max-time 120", "--retry-max-time 120 --retry-max-time 1"), "--retry-max-time is given 2 times"),
        (("--connect-timeout 10", "--connect-timeout 10 --connect-timeout 0"), "--connect-timeout is given 2 times"),
        (("--retry-delay 5", "--retry-delay 5 --retry-delay 5"), "--retry-delay is given 2 times"),
    ],
)
def test_a_repeated_required_option_is_refused_because_curl_uses_the_last_one(change, expected) -> None:
    old, new = change
    ((_, words),) = curl_commands(GOOD.replace(old, new, 1))
    assert any(expected in problem for problem in problems(words))


def test_the_scanner_reads_quotes_as_the_shell_does() -> None:
    ((_, words),) = curl_commands('          curl --fail "--retry" \'3\' "https://example.invalid/a b" --output x\n')
    assert words == ["curl", "--fail", "--retry", "3", "https://example.invalid/a b", "--output", "x"]


def test_an_unbalanced_quote_is_a_violation_not_a_crash() -> None:
    ((_, words),) = curl_commands('          curl --fail "--retry 3 --output x\n')
    assert words == ["<unreadable: unbalanced quote>"] and problems(words)


def test_a_curl_in_a_comment_or_a_word_is_not_a_command() -> None:
    assert curl_commands("# nothing here\n  echo securecurl-is-not-curl\n") == []


def test_a_commented_out_curl_is_not_a_command_but_the_real_one_next_to_it_is() -> None:
    text = (
        "        run: |\n"
        "          # curl --fail --location --silent --show-error --retry 3 https://example.invalid/old\n"
        "          curl --fail --location --silent --show-error https://example.invalid/new  # curl is not run twice\n"
        '          echo "done" # curl --fail again\n'
    )
    ((number, words),) = curl_commands(text)
    assert number == 3 and "https://example.invalid/new" in words and "again" not in words


def test_a_commented_out_start_hides_its_continuation_lines_too() -> None:
    commented = GOOD.replace("          curl --fail", "          # curl --fail")
    assert commented != GOOD and curl_commands(commented) == []


def test_a_hash_inside_a_word_is_not_a_comment() -> None:
    ((_, words),) = curl_commands("          curl --fail https://example.invalid/a#frag --output x\n")
    assert "https://example.invalid/a#frag" in words and "x" in words


# --------------------------------------- the loop around each download: retry a transfer that broke off, nothing else

LOOP = re.compile(r"^ +attempt=0\n.*?^ +done\n", re.MULTILINE | re.DOTALL)


def retry_loops(text: str) -> list[str]:
    return [match.group(0) for match in LOOP.finditer(text)]


def test_each_real_download_sits_in_one_bounded_retry_loop_for_broken_transfers() -> None:
    loops = retry_loops(REAL)
    assert len(loops) == 2, loops
    for loop in loops:
        assert 'case "$status" in 18 | 56) ;; *) exit "$status" ;; esac' in loop  # only these two, nothing else
        assert '[ "$attempt" -lt 3 ] || exit "$status"' in loop  # at most three more attempts
        assert 'rm -f "$archive"' in loop and "sleep 5" in loop  # the partial file goes, a short wait comes
        assert loop.count("until curl --fail") == 1 and "--retry-all-errors" not in loop
        assert "sha256sum" not in loop and "tar -xzf" not in loop  # the checks and the extraction stay outside


def find_bash() -> str | None:
    """A bash that runs scripts: Git Bash next to git, or a Linux or macOS shell; never the Windows WSL launcher."""
    candidates: list[str | None] = []
    git = shutil.which("git")
    if git:
        for parent in list(Path(git).resolve().parents)[:3]:
            candidates += [
                str(parent / rel) for rel in ("bin/bash.exe", "usr/bin/bash.exe") if (parent / rel).is_file()
            ]
    candidates.append(shutil.which("bash"))
    for candidate in candidates:
        if not candidate or {"system32", "syswow64", "sysnative", "windowsapps"} & {
            part.lower() for part in PureWindowsPath(candidate).parts
        }:
            continue
        try:
            done = subprocess.run([candidate, "-c", "echo ok"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0 and done.stdout.strip() == "ok":
            return candidate
    return None


BASH = find_bash()
FAKE_CURL = """state="$FAKE_DIR/codes"; calls="$FAKE_DIR/calls"
out=""; prev=""
for arg in "$@"; do if [ "$prev" = --output ]; then out="$arg"; fi; prev="$arg"; done
code=$(head -n 1 "$state"); tail -n +2 "$state" > "$state.next"; mv "$state.next" "$state"
if [ -e "$out" ]; then echo "curl: a leftover file" >> "$calls"; else echo "curl: a clean start" >> "$calls"; fi
if [ "$code" = 0 ]; then echo complete > "$out"; else echo partial > "$out"; fi
exit "$code"
"""
FAKE_SLEEP = 'echo "sleep $*" >> "$FAKE_DIR/calls"\n'


def run_loop(loop: str, codes: list[int], tmp_path: Path) -> tuple[int, list[str], Path]:
    """Run one real loop with a fake `curl` that exits with each code in turn, and a `sleep` that only writes down
    that it was asked. Returns the shell's exit code, what the fakes saw, and the archive path."""
    fake = tmp_path / "fake"
    fake.mkdir()
    for name, body in (("curl", FAKE_CURL), ("sleep", FAKE_SLEEP)):
        (fake / name).write_bytes(body.encode("utf-8"))
    (fake / "codes").write_text("".join(f"{code}\n" for code in codes), encoding="utf-8")
    archive = tmp_path / "gitleaks.tar.gz"
    # shell functions win over any real `curl` or `sleep` on the PATH (Git Bash puts its own first)
    fakes = 'curl() { "$BASH" "$FAKE_DIR/curl" "$@"; }\nsleep() { "$BASH" "$FAKE_DIR/sleep" "$@"; }\n'
    script = f"set -e -o pipefail\n{fakes}archive='{archive.as_posix()}'\n{loop}echo reached the next step\n"
    env = {**os.environ, "FAKE_DIR": fake.as_posix()}
    done = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env, timeout=120)
    calls = (fake / "calls").read_text(encoding="utf-8").splitlines() if (fake / "calls").exists() else []
    assert ("reached the next step" in done.stdout) == (done.returncode == 0), (done.stdout, done.stderr)
    return done.returncode, calls, archive


NEEDS_BASH = pytest.mark.skipif(BASH is None, reason="no usable bash here (Git Bash, or a Linux or macOS shell)")
BOTH_LOOPS = pytest.mark.parametrize("which", [0, 1], ids=["first-step", "second-step"])


@NEEDS_BASH
@BOTH_LOOPS
@pytest.mark.parametrize(
    ("codes", "exit_code", "curl_calls"),
    [
        ([0], 0, 1),
        ([18, 0], 0, 2),
        ([56, 0], 0, 2),
        ([18, 56, 18, 0], 0, 4),
        ([56, 56, 56, 56], 56, 4),  # three more attempts, then the step fails with the last code
        ([18, 18, 18, 18, 0], 18, 4),  # the bound: a fifth try is never made
    ],
)
def test_a_transfer_that_broke_off_is_retried_three_more_times_from_a_clean_file(
    tmp_path, which, codes, exit_code, curl_calls
) -> None:
    code, calls, archive = run_loop(retry_loops(REAL)[which], codes, tmp_path)
    assert code == exit_code
    curls = [line for line in calls if line.startswith("curl")]
    assert len(curls) == curl_calls and all(line == "curl: a clean start" for line in curls), calls  # partial removed
    assert calls.count("sleep 5") == curl_calls - 1  # one short wait before each retry, none after the last try
    if exit_code == 0:
        assert archive.read_text(encoding="utf-8").strip() == "complete"  # the finished download is what is left


@NEEDS_BASH
@BOTH_LOOPS
@pytest.mark.parametrize("failure", [22, 60, 35, 7, 28, 1])
def test_every_other_failure_ends_the_step_at_once_without_a_retry_or_a_wait(tmp_path, which, failure) -> None:
    """22 is a 404 (or any HTTP error) under `--fail`, 60 a bad certificate, 35 and 7 a failed handshake or connection,
    28 a timeout that `--retry` has already retried: none is a transfer that broke off, so none is tried again here."""
    code, calls, _archive = run_loop(retry_loops(REAL)[which], [failure, 0], tmp_path)
    assert code == failure
    assert calls == ["curl: a clean start"], calls  # one call, no sleep


@NEEDS_BASH
@BOTH_LOOPS
def test_a_retry_that_then_meets_another_kind_of_failure_stops_there(tmp_path, which) -> None:
    code, calls, _archive = run_loop(retry_loops(REAL)[which], [18, 22, 0], tmp_path)
    assert code == 22 and [line for line in calls if line.startswith("curl")] == ["curl: a clean start"] * 2
    assert calls.count("sleep 5") == 1


def test_the_loop_scanner_finds_nothing_where_there_is_no_loop() -> None:
    assert retry_loops(GOOD) == []


def test_a_semicolon_glued_to_the_last_word_is_split_off_so_a_flag_before_it_is_seen() -> None:
    ((_, words),) = curl_commands('          until curl --fail --output "$archive" --retry-all-errors; do\n')
    assert words[-3:] == ["--retry-all-errors", ";", "do"] and "--retry-all-errors" in words
