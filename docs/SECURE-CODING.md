# Writing secure code: the agenttalk standard, version 1

Status: accepted for a pilot. Version 1, written on 2026-10-28 for work order tk-6f367777d3cc, after the challenge ch-651fb888 returned RESHAPE and the lead accepted it. Revised in fix round tk-4f0f5ca4e611 after the read tk-1297b29787e4, in fix round tk-deae55f9bdd9 after a read by the second team's reviewers (2026-10-29), and in fix round tk-9830ec5cc9c4 after the delta read tk-b1d90a6c8abc. The operator accepted the standard and its pilot on 2026-10-29. The pilot showed a difference, and on 2026-10-30 the operator accepted its cost and read its kill signal as not fired; the standard is now loaded by the skills, the contributor guide and the pull request template, as the [rollout plan](https://github.com/zoolok17/agenttalk/blob/master/docs/design/secure-coding-rollout.md) says. Part 1 is also shipped as a copy inside the skills, and two repository tests keep the copy equal to this page. Owner: the lead. Next review: 28 April 2027, or when NIST finalises its new SSDF version, whichever comes first.

## In plain words

This page says how to keep security in mind while you write code, not only when the code is tested afterwards. Before you write, you note in three lines where something from outside reaches your change, how it could be misused, and what the code must do instead. While you write, you choose protections from a short list of ten security principles, S1 to S10. When you hand the work over, you point each choice at the code and at a check that ran; the reader who already checks every change checks these claims too. If you are sending a single pull request, the next section is the short route through the same rules. Part 1 holds the general rules, written so that any project could use them; part 2 says how they apply in agenttalk, what already exists, and how they map to the NIST and OWASP guidance. The standard adds no approval and no new scanner, needs no private tools, and claims to be in line with that guidance, not to comply with it.

## If you are sending one pull request

This section is the short route through the same rules. It needs nothing private.

1. **Before you write any code, decide whether your change touches outside input.** Outside input is anything your code did not make itself: a message, a file someone else can write, a command-line argument or setting, a web request, an AI tool's answer. Section 1 has the full list.
2. **No outside input: one line, and you are done with this page.** Write it before you start, in the issue or a draft pull request, and copy it under "How you checked it" in your pull request description. The first example below shows it.
3. **Outside input: write the three-line note first,** before any code, in the issue or a draft pull request: where the outside input reaches your change, how it could be misused, and what the code must do instead (step 1 in section 2).
4. **Pick the rules your change brings into play** from the ten in section 3 (S1 to S10, one paragraph each), and build each protection with a test that fails without it.
5. **Under "How you checked it", write one line for each rule you picked,** in one of four forms, one form per line, and an `executed` line says only what its test covers (step 3 in section 2):
   - `executed <command> -> pass`: the test that shows the protection;
   - `checked by reading - <reason>`: only for the few promises that section 3 marks as checked by reading;
   - `does not apply - <reason>`: the rule has nothing to protect here;
   - `open - <reason>, <owner>`: anything not settled yet, such as who may see the data.
6. **Run the tests your lines name,** with `python -m pytest <your test file>` from the top of your clone, as the [contributing guide](../CONTRIBUTING.md#before-you-send-a-pull-request) describes. That is all you run yourself. CI picks the checks for your pull request: the full set, or the lighter documentation path, as [the contributing guide explains](../CONTRIBUTING.md#which-checks-a-change-gets). The full set includes the development gate on Linux, macOS and Windows and the security scanners (ruff's `S` rules, bandit, semgrep, gitleaks, pip-audit and zizmor); CodeQL runs on every pull request. The maintainers run the client-reference check that a fork cannot run. You need none of these tools installed.

**A complete example with no outside input** (a change that renames an internal helper and updates its tests):

```text
security-note: no trust boundary - renames an internal function; no input, file, process, network or permission handling changes
```

**A complete example with outside input** (the `read_report` function of section 5, used by a page that takes the report's name from a web request):

```text
security-note: issue #<n>, comment of <date and time>, written before any code
  boundary: a report name from a web request reaches a file read
  misuse: a name with "..", an absolute path or a link reads a file outside the reports folder, or a named pipe holds the request
  safe behaviour: refuse a name that leads outside the folder, and anything that is not a plain file with a single name, at once and with a plain error
security S3: executed python -m pytest tests/test_reports.py -> pass on <commit> (names with "..", an absolute path, an empty name, a hard link and a symbolic link to an outside file, each refused, and a named pipe refused at once; the symbolic-link and named-pipe cases are skipped where the system cannot create them)
security S8: executed the same run -> pass (a report over the size limit refused)
security S5: does not apply - the name is never shown back; the error text is fixed
security S4: open - who may read which report is not decided yet; owner: <who decides>
```

The reader who reads your pull request asks for anything that is missing.

## Contents

Part 1. The general rules
1. [When it applies](#1-when-it-applies)
2. [The three steps](#2-the-three-steps)
3. [The ten principles](#3-the-ten-principles)
4. [Code written by an AI agent](#4-code-written-by-an-ai-agent)
5. [Examples in Python](#5-examples-in-python)
6. [Examples in Java](#6-examples-in-java)

Part 2. In agenttalk
7. [How the rules apply in agenttalk](#7-how-the-rules-apply-in-agenttalk)
8. [What already exists and stays](#8-what-already-exists-and-stays)
9. [How this maps to outside frameworks](#9-how-this-maps-to-outside-frameworks)
10. [Owner, versions and review](#10-owner-versions-and-review)

## Part 1. The general rules

Part 1 names no project, path or tool of agenttalk's own, so that it can be shipped with the skills to any project (see the rollout plan). Two hidden marker lines show exactly where it starts and ends.

<!-- secure-coding:general:begin -->

## 1. When it applies

- **Every change to code,** including tests, build scripts and CI workflows.
- **Text that agents act on counts as code here:** skills, runbooks, agent instructions, prompts, and policy files that change what an agent does or may do. Text that only describes (a guide, a design, a changelog) needs nothing from these rules.
- **Who does it:** whoever writes the change (the builder). The reader who already reviews every change checks the builder's claims. No one else is added, and no approval is added.
- **Small changes stay small.** A change that touches no trust boundary needs one line that says so, and why.

A **trust boundary** is a place where something the code does not control reaches code that acts. Things the code does not control include:

- input from people, from other programs and from other agents;
- files and folders that someone else can write to;
- command-line arguments, environment variables and configuration files;
- network requests and responses;
- whatever an AI tool or a model returns;
- repository content: branch names, file names, commit messages;
- downloaded files.

Code **acts** when it starts a process, reads, writes or deletes a file, opens a network connection, shows a page, shows or exports data, merges, releases or deletes work, or spends money. A project can keep its own list of boundaries next to these rules.

## 2. The three steps

### Step 1: before you write, the security note

Write three short lines before you write the code:

1. **Boundary:** which outside source reaches which action in your change.
2. **Misuse:** the most plausible way a person, or a confused agent, could misuse it.
3. **Safe behaviour:** what the code must do instead (refuse, escape, limit), said so that a test can check it. Name every refusal the code adds (each limit and each rejected input) and what each costs a legitimate user, so that the reader can weigh it.

If the change touches no trust boundary, write one line instead: `security-note: no trust boundary - <reason>`. A change that moves, merges or simplifies code at a boundary (a path check, a permission check) touches that boundary, even when its behaviour should not change.

Put the note somewhere that records when it was written: a comment on the issue, a message on the task's thread, or the description of a draft pull request. A recorded time shows only that the note existed before the code was committed, not that it was written before the code. When the order matters, the person who assigns the work asks for the note as a separate first step, and starts the coding only after receiving it. If you could only write the note afterwards, say so in the note. A late note that says it is late is acceptable, and it counts as late; a note that pretends to be early is not acceptable.

### Step 2: while you write, choose the protections

Go through the ten principles in section 3. For each one your boundary needs, choose a concrete protection, build it, and add a test that would fail without it. Where a principle's promise cannot be tested in practice, section 3 says so and says what to check by reading instead. Prefer a safe interface that already exists (a list of arguments instead of a shell command, a parameterised query, the standard library's comparison function) to code of your own. If the note turns out to be wrong, correct it and say what changed.

### Step 3: at handoff, point each choice at code and at a check that ran

In the handoff you already write, add one line for each principle you considered:

- `security S<n>: executed <command or run id> -> pass <what it shows>`: the protection is built, and a check that ran on the exact commit you hand over shows it.
- `security S<n>: checked by reading - <what was read and why no test>`: only for a promise that section 3 marks as checked by reading.
- `security S<n>: does not apply - <reason>`: the principle has nothing to protect in this change, and why.
- `security S<n>: open - <reason>, <owner>`: not done yet, and who will do it. A protection whose check fails is handed over as open, with the failing output.

**One form per line.** Each line takes exactly one of the four forms and starts with it. A principle that is partly built and partly not gets two lines: an `executed` line for the part a check covers, and an `open` line, starting with the word `open`, for the rest. Never mix `executed` and `open` in one line.

**An `executed` line names only what its check covers.** Say what the command shows, no more. If the test reads only the input stream, the line says "input stream", not "the stream"; if markup was tried in three fields, the line names those three, not "each field". A claim wider than its check is a wrong line, even when the code does keep the protection: the reader cannot tell it from a protection that is missing.

Add the note itself as `security-note: <where it is and when it was written>`.

**How many lines.** Not ten. A change with no trust boundary has one line in total: its note. A change with a boundary has a line for each principle that the boundary brings into play, and a `does not apply` line only where a reader would expect a principle to matter. The example below has three principle lines.

A team that uses the definition-of-done skill and its evidence profiles puts these lines in fields that already exist; no new field is needed:

| Field (the `production-handoff` profile) | What goes there |
|---|---|
| `evidence` | the `security-note:` line and every `security S<n>:` line, one entry each |
| `tests_executed` | the commands or run ids that the lines name |
| `residual_risk` | every `open` line, and anything the note could not rule out |
| `required_review_lenses` | `security: <reader's seat>` when the note names a boundary |

A contributor without the team's tools puts the same lines in the pull request description.

**What the reader checks.** The reader who already reads every change (with the security pass of the `review-code` skill) also checks that:

- the note's recorded time comes before the first code commit, or the note says it is late. This shows that the note existed before the code was committed, not that it shaped the code; only a separate first step, as in step 1, shows that;
- the boundary is right, and the change has no other boundary the note misses;
- each `executed` check exists and ran on the exact commit handed over. The reader removes the protection (a copy of the code with that one change) and sees the named command fail. Where that cannot be tried, the reader says why. A line whose command stays green without the protection, or whose words claim more than the command covers, is a finding;
- each `checked by reading` and `does not apply` reason is true for this change;
- the code does what each line says.

The reader records what they checked as `security S<n>: checked <how>` in their own evidence (the `review-result` profile), with `risk_class=security` when the note names a boundary. A filled-in list proves neither that the protections were applied nor that the code is secure. The code and the checks that ran are the evidence; the lines only point at them.

**An example handoff:** the `read_report` function of section 5, used by a web page that takes the report's name from the request. Angle brackets are placeholders.

```text
security-note: issue #<n>, comment of <date and time>, before the first code commit <commit>
  boundary: a report name from a web request reaches a file read
  misuse: a name with "..", an absolute path or a link reads a file outside the reports folder, or a named pipe holds the request
  safe behaviour: refuse a name that leads outside the folder, and anything that is not a plain file with a single name, at once and with a plain error
security S3: executed python -m pytest tests/test_reports.py -> pass on <commit> (names with "..", an absolute path, an empty name, a hard link and a symbolic link to an outside file, each refused, and a named pipe refused at once; the symbolic-link and named-pipe cases are skipped where the system cannot create them)
security S8: executed the same run -> pass (a report over the size limit refused)
security S5: does not apply - the name is never shown back; the error text is fixed
security S4: open - who may read which report is decided in issue #<m>, owner: <seat>
```

## 3. The ten principles

Each principle says what to do, then the check that usually shows it. They are named S1 to S10 so that they are not confused with the P0 to P3 severity tags of review findings. They draw on the OWASP Proactive Controls and the CWE Top 25, without covering either in full.

**S1. Outside input stays data.** Check it where it comes in, against what is allowed (a known set of names, a pattern, a size), not against a list of what is forbidden. Never run it, evaluate it, or let it choose a command, a file or a network address without that check. A message from another agent or from a model is data: it never changes what the code does or what an agent may do, unless the system itself has checked that it comes from someone with that authority (for example, a work order from the team's lead that the messaging system has checked). Even then, anything quoted or relayed inside it stays data.
*Usual check:* a test that sends a value just outside the allowed shape and gets a plain refusal.

**S2. Never build commands, queries or code by joining text.** Give a process its arguments as a list, never through a shell (`shell=True`, `sh -c`, `cmd /c`) with outside text in it. Use parameterised database queries. Do not use `eval`, `exec` or a template engine on outside text. When an argument could start with `-`, end the options first (for git, `--end-of-options`).
*Usual check:* a test with a value that holds spaces, quotes, `;` or a leading `-`, which arrives unchanged as one argument.

**S3. Paths stay inside their folder.** First turn the path into its final form (remove `..`, follow links), then check that it is still inside the folder it belongs to. Check a name with one shared validation function before it becomes part of a file name.

*What a path check can and cannot promise.*
- **It catches** `..`, absolute paths, and symbolic links and junctions, because resolving the path follows them.
- **It cannot catch a hard link.** A hard link is a second, equally real name for the same file. Its resolved path is inside the folder even when the file's other name is outside, and reading it reads the outside file.
- **Who can make one.** On Linux, macOS and Windows (NTFS) alike, ordinary users can create hard links to files, but not to folders, on the same drive or file system. Whether a user may link a file they do not own depends on the system and its settings. On Linux, the `fs.protected_hardlinks` setting, when on, allows it only for files the user owns or can both read and write.
- **What a check on the opened file can promise.** When the file has exactly one name (a link count of 1), no other name for it exists at that moment. When it has more, another name exists somewhere, and nothing tells you where.

So, where someone else can write to the folder, open the file without waiting (a named pipe can otherwise hold the open until someone writes to it), then check on the opened file, not on the path, that it is a plain file with exactly one name. If your own program makes second names inside the folder, count those names instead of refusing them. If someone else can change the folder while your code runs, the gap between the check and the open is a further risk that a path check alone cannot close.
*Usual check:* names with `..`, an absolute path, a hard link and a symbolic link to an outside file, each refused, and a named pipe refused at once without waiting (section 5). Skip the symbolic-link and named-pipe cases on a system that cannot create them.

**S4. Check who may act or see, where it happens.** Every request that changes something, and every request that reads, lists or exports data that not everyone may see, checks that the actor may do it, at the place where it happens. It takes the actor's identity from the trusted side (the server, the team's own list of members), never from what the caller says about itself.
*Usual check:* a request for data that belongs to someone else, and the same change asked for by an actor who may not make it, each refused, with nothing shown and nothing changed.

**S5. Escape output for where it goes.** Outside text is escaped for its destination: HTML, Markdown, a terminal, a log line.
*Usual check:* a message that contains `<script>` is shown as text.

**S6. Keep secrets secret, and use proven cryptography.** No secret in code, logs, error messages, messages between agents, commits or test output: name where a secret lives, never its value. Compare secrets in constant time, make tokens from a strong random source, and use the standard library's cryptography, never your own. Treat private and client data the same way.
*Usual check:* a test that the comparison goes through the constant-time function (section 5 shows one), and a test that the error for a wrong key does not contain the key.
*Checked by reading:*
- **That the comparison really takes the same time.** A timing test on a shared machine is too noisy to show it.
- **That tokens come from a strong random source.** No test can tell strong randomness from weak by looking at a few values. Scanner rules such as bandit's B311 and ruff's S311 catch many uses of a weak generator, but not all: ruff's rule did not flag `random.getrandbits` when we tried it.

**S7. Safe by default, and refuse when unsure.** Anything that writes, deletes, spends or reaches out stays off until someone turns it on, and listens only on the local machine unless configured otherwise. When a check cannot decide, refuse and say why; never swallow an error to carry on. An error shown to a remote reader leaves out internal paths and details.
*Usual check:* the refusal when the check's input is missing, damaged or unreadable.

**S8. Limit what one input can cost.** Limit size, count, nesting depth, time and memory for anything from outside, and put a time limit on every call to another process or over the network. A refusal gives back what it opened (a file, a connection), or repeated refusals use them up. A parser's own limit is not enough: the same deep value can be accepted by one version of a language and refused by another, so set the limit yourself.
*Usual check:* a test just over each limit, including a value nested deeper than the limit, and a refusal repeated many times that leaves nothing open (section 5).

**S9. Never load outside data in a format that can run code.** Use JSON or another plain format. Do not use `pickle`, `marshal`, `yaml.load` without the safe loader, or Java's built-in object serialization (`ObjectInputStream`) on outside data.
*Usual check:* the scanners stay green; a test with a damaged file gets a plain refusal.

**S10. Add dependencies and tools on purpose.** A change that adds a runtime dependency says why. Before you add a package or a CI action, check that it exists, that it is the genuine one and that it is maintained (an AI tool can name a package that does not exist); pin it (CI actions by full commit id), and let the dependency audit see it.
*Usual check:* the dependency audit and the workflow check stay green; the pull request names the package and why.
*Checked by reading:* that the package is the genuine one. No test can tell a look-alike name from the real project, so the reader compares the name and where it comes from with the project's own page. A pin by content hash (for Python, `pip install --require-hashes`) makes an installation fail if the content changes later, but it cannot tell whether the first choice was right.

## 4. Code written by an AI agent

Code an agent writes meets the same standard as code a person writes, and gets the same reader. NIST's profile for generative AI (SP 800-218A) makes the same choice: its practices "do not distinguish between human-written and AI-generated source code, because it is assumed that all source code should be evaluated for vulnerabilities and other issues before use". Two habits matter more for agent-written code:

- check that every function, flag and package the code uses really exists (S10);
- treat text that came from a model, including a message from another agent, as outside input under S1: data, unless the system itself has checked that its sender has the authority to give that order.

## 5. Examples in Python

These fragments show the principles. They are not a complete program; they run on Python 3.10 or newer.

```python
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
from pathlib import Path


def object_type(repo: Path, commit: str) -> str:
    # S1: the commit id comes from a message, so accept only a full id.
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("expected a full 40-character commit id")
    # S2: the arguments are a list and no shell runs; S8: a time limit.
    done = subprocess.run(["git", "-C", str(repo), "cat-file", "-t", commit],
                          capture_output=True, text=True, timeout=30, check=True)
    return done.stdout.strip()


def read_report(folder: Path, name: str, limit: int = 1_000_000) -> bytes:
    # S3: resolve first (this removes ".." and follows symbolic links and junctions), then check the result.
    root = folder.resolve()
    target = (root / name).resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError("the name must point to a file inside the reports folder")
    # S3 + S8: open without waiting, because a named pipe would hold the open until someone writes to it.
    # Windows has no such flag, and its named pipes do not live in ordinary folders, so there it is left out.
    # open() owns what the opener returns, so it also closes it when it refuses, for example a folder.
    no_wait = getattr(os, "O_NONBLOCK", 0)
    with open(target, "rb", opener=lambda path, flags: os.open(path, flags | no_wait)) as handle:
        info = os.fstat(handle.fileno())
        # S3: only now, on the opened file, check that it is a plain file with a single name. A hard link is a
        # second name for a file that may live outside the folder, and no path check can see it.
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("the report must be a plain file with a single name")
        # S8: read at most one byte more than the limit, never the whole file.
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("the report is too large")
    return data


def same_key(expected: bytes, given: bytes) -> bool:
    # S6: the comparison takes the same time however much of the key matches.
    return hmac.compare_digest(expected, given)


def new_token() -> str:
    # S6: a strong random source; never the random module.
    return secrets.token_hex(32)


MAX_DEPTH = 20


def nesting_depth(value: object) -> int:
    # Counted with a list, not by recursion, so a deep value cannot exhaust the call stack.
    deepest, pending = 0, [(value, 1)]
    while pending:
        item, depth = pending.pop()
        deepest = max(deepest, depth)
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return deepest


def load_settings(text: str) -> dict:
    # S8: refuse oversized input before parsing it, and deep nesting after.
    if len(text) > 64_000:
        raise ValueError("the settings file is too large")
    try:
        data = json.loads(text)  # S9: JSON cannot run code while it loads.
    except RecursionError:
        raise ValueError("the settings are nested too deeply") from None
    if not isinstance(data, dict):
        raise ValueError("the settings must be a JSON object")
    if nesting_depth(data) > MAX_DEPTH:
        raise ValueError("the settings are nested too deeply")
    return data
```

The checks that go with them (pytest). Each one fails if its protection is removed:

```python
import hmac
import os
import signal

import pytest


@pytest.mark.parametrize("name", ["../secret.txt", "a/../../secret.txt", ""])
def test_a_name_outside_the_reports_folder_is_refused(tmp_path, name):
    with pytest.raises(ValueError):
        read_report(tmp_path / "reports", name)


def test_an_absolute_path_is_refused(tmp_path):
    (tmp_path / "secret.txt").write_text("secret")
    with pytest.raises(ValueError):
        read_report(tmp_path / "reports", str(tmp_path / "secret.txt"))


def test_a_hard_link_to_a_file_outside_is_refused(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    os.link(tmp_path / "secret.txt", reports / "alias.txt")
    with pytest.raises(ValueError):
        read_report(reports, "alias.txt")


def test_a_symbolic_link_to_a_file_outside_is_refused(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    try:
        (reports / "alias.txt").symlink_to(tmp_path / "secret.txt")
    except OSError:
        pytest.skip("this system does not let the test create symbolic links")
    with pytest.raises(ValueError):
        read_report(reports, "alias.txt")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this system has no named pipes")
def test_a_named_pipe_is_refused_at_once(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    os.mkfifo(reports / "pipe")
    signal.alarm(5)  # if the open waits for a writer, the alarm stops the test run after 5 seconds
    try:
        with pytest.raises(ValueError):
            read_report(reports, "pipe")
    finally:
        signal.alarm(0)


FD_FOLDER = next((d for d in ("/proc/self/fd", "/dev/fd") if os.path.isdir(d)), None)


@pytest.mark.skipif(FD_FOLDER is None, reason="this system cannot list its open descriptors")
def test_refusing_a_folder_many_times_leaves_nothing_open(tmp_path):
    reports = tmp_path / "reports"
    (reports / "folder").mkdir(parents=True)
    before = len(os.listdir(FD_FOLDER))
    for _ in range(100):
        with pytest.raises(OSError):
            read_report(reports, "folder")
    assert len(os.listdir(FD_FOLDER)) == before


def test_a_report_is_read_and_an_oversized_one_refused(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "a.txt").write_bytes(b"x" * 10)
    assert read_report(reports, "a.txt") == b"x" * 10
    with pytest.raises(ValueError):
        read_report(reports, "a.txt", limit=9)


def test_keys_are_compared_by_the_constant_time_function(monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    assert same_key(b"abc", b"abc") and not same_key(b"abc", b"abd")
    assert len(calls) == 2


def test_settings_nested_too_deeply_are_refused():
    for depth in (MAX_DEPTH + 1, 2_000, 20_000):
        with pytest.raises(ValueError):
            load_settings('{"a": ' + "[" * depth + "]" * depth + "}")
    assert load_settings('{"a": {"b": [1, 2]}}') == {"a": {"b": [1, 2]}}
```

## 6. Examples in Java

A generic example, not taken from any project. These are fragments: they show the calls, not a whole program. Version 1's Java fragments have been checked by reading only; they are compiled and tested before the rules are adopted.

```java
// S1 + S2: accept only a full commit id, then pass each argument separately; no shell runs.
if (!commitId.matches("[0-9a-f]{40}")) {
    throw new IllegalArgumentException("expected a full 40-character commit id");
}
Process git = new ProcessBuilder("git", "-C", repo.toString(), "cat-file", "-t", commitId).start();
// S8: a time limit.
if (!git.waitFor(30, TimeUnit.SECONDS)) {
    git.destroyForcibly();
    throw new IllegalStateException("git did not answer within 30 seconds");
}

// S2 + S4: a parameterised query; the owner comes from the signed-in session, not from the request,
// so a request for someone else's report finds nothing.
try (PreparedStatement query = connection.prepareStatement(
        "SELECT title FROM report WHERE id = ? AND owner = ?")) {
    query.setLong(1, reportId);
    query.setString(2, session.userName());
    try (ResultSet rows = query.executeQuery()) {
        // read the rows
    }
}

// S3: resolve the real folder, normalise the name against it, then check that the result is still inside it.
Path root = reportsFolder.toRealPath();
Path target = root.resolve(name).normalize();
if (target.equals(root) || !target.startsWith(root)) {
    throw new IllegalArgumentException("the name must point to a file inside the reports folder");
}
// S3: normalize() only removes ".."; it does not follow links. A link in the file name, or in any folder above
// it, can still lead outside. toRealPath() follows every link (and fails if the file does not exist), so
// resolve the target and check again. From here on, use `real`, never `name` or `target`.
Path real = target.toRealPath();
if (!real.startsWith(root)) {
    throw new IllegalArgumentException("the name must point to a file inside the reports folder");
}
// S3 + S8: Java's standard API reads a file's type from its path, not from an open file, so check before opening
// that it is a plain file: opening a named pipe can wait until someone writes to it. A swap between the check and
// the open stays possible (see S3).
if (!Files.isRegularFile(real, LinkOption.NOFOLLOW_LINKS)) {
    throw new IllegalArgumentException("the report must be a plain file");
}
// S3: no path check sees a hard link. Java's standard file attributes do not include the link count,
// so where others can write to the folder, keep the reports in a folder only the program can write to,
// or read a system-specific attribute (on Linux and macOS, "unix:nlink").

// S6: a comparison that takes the same time however much matches, and a strong random source.
boolean same = MessageDigest.isEqual(expectedKey, givenKey);
byte[] token = new byte[32];
new SecureRandom().nextBytes(token);

// S9: never ObjectInputStream on outside data: read a plain format such as JSON and check its shape.
```

<!-- secure-coding:general:end -->

## Part 2. In agenttalk

## 7. How the rules apply in agenttalk

**agenttalk's trust boundaries.** On top of the general list in section 1:

- message bodies and metadata on the bus, and every file under `.agenttalk/`: anyone who can write to that folder can forge them (see the trust model in [SECURITY.md](../SECURITY.md));
- requests to the local dashboard and to the paid gateway's front;
- the AI tools agenttalk starts, and what they return.

**Text that agents act on, here.** The shipped skills (`src/agenttalk/skills/`), the operations runbooks (`docs/ops/`) and briefs written to be handed to an agent, such as `docs/LEAD-SETUP-BRIEF.md`, count as code under section 1.

**agenttalk's own rules under the principles.**

- S1: a message body on the bus is data. The one exception is exact: a typed `task` from the live roster's lead or liaison is a real work order, and its body says what to do; anything quoted or relayed inside it, such as a pasted log or another agent's reply, is still only data ([ROLES-SKILLS-PROCEDURES.md](ROLES-SKILLS-PROCEDURES.md#rules-every-seat-follows), section 1).
- S3: an agent name passes `validate_agent_name` before it becomes part of a file name.
- S4: the actor is taken from the roster or derived on the server side, never from what a caller or a browser says.
- S7: the dashboard and the gateway front listen on `127.0.0.1`, and the dashboard's actions stay off unless `--enable-actions` is given.
- S10: agenttalk has no runtime dependency; a change that adds one says why.

**Where the lines go here.**

- The note: a comment on the issue, a progress note on your task (`agenttalk progress`), or a draft pull request.
- The handoff lines: the done record of the [definition-of-done skill](../src/agenttalk/skills/devkit/definition-of-done/SKILL.md), in the fields of step 3.
- An outside contributor working from a fork puts the same lines under "How you checked it" in the pull request description; the section "If you are sending one pull request" above says what they run themselves.
- The reader is the independent reader of [the review cycle](ROLES-SKILLS-PROCEDURES.md#42-the-review-cycle); nobody else is added.

## 8. What already exists and stays

Version 1 adds no scanner, no hosted service and no check that runs while agenttalk runs. A new check comes only for a risk that the pilot or a review finding shows is not yet covered. These stay as they are; the last column names the practice of the NIST Secure Software Development Framework (SSDF, section 9.1) that each one serves.

| What | Where it runs | What it finds or does | SSDF practice |
|---|---|---|---|
| ruff's `S` rules | the development gate, on the CI leg that runs the static checks (Linux, Python 3.12), on every pull request that takes the full path, and weekly | Python patterns known to be unsafe (the rules come from bandit), in `src/` and `tests/`. `pyproject.toml` turns off S101, S603 and S607 everywhere, and also S105 and S106 in tests. With S603 and S607 off, ruff does not report a `subprocess` call that passes a list of arguments; S602 still reports one with `shell=True` | PW.5, PW.7 |
| bandit | the same | Python security patterns in `src/` (the shipped skills are left out) | PW.7 |
| semgrep | the same, with the `p/python` and `p/security-audit` rule sets and three project rules in `.semgrep/agenttalk.yml` | unsafe patterns, and pattern checks for three agenttalk rules: agent names are validated before they become file names, messages are written only through the store, and a message body never reaches code execution. A pattern check finds the forms it knows; it does not prove the rule holds everywhere | PW.5, PW.7 |
| gitleaks | the development gate, over the full history, and on the documentation-only path | credentials committed by mistake | PW.7, PS.1 |
| pip-audit (strict) | the development gate | published vulnerabilities in the packages of a clean runtime installation: the gate installs the built wheel into a fresh environment, lists what is installed there (`pip freeze`, with agenttalk itself removed), and audits that list without resolving anything further. **Gap:** the development tools (pytest, ruff, bandit and the rest) are not in that list, so no gate check fails on a vulnerable development tool | PW.4, RV.1 |
| zizmor | the development gate, and on the documentation-only path | unsafe GitHub Actions workflows | PO.3, PO.5 |
| CodeQL (`security-extended`) | the `security` workflow, on every pull request and weekly | data flows from outside input to dangerous calls | PW.7, RV.1 |
| The client-reference check | the `security` workflow; for a fork, the lead runs it | private client names in added text and in the whole tree | none: it protects other people's private data, which the SSDF does not cover |
| The security pass of `review-code` (`references/security.md`) | the independent reader, on every change that touches a sensitive area | an OWASP-based checklist: access control (including object-level checks on reads), input, output, secrets, errors, dependencies, paths, resources | PW.7 |
| [SECURITY.md](../SECURITY.md) | public | how to report a vulnerability privately; the trust model; four threat scenarios | RV.1, PW.1 |
| The review cycle and the merge rules ([CONTRIBUTING.md](../CONTRIBUTING.md#the-pull-request-checklist)) | every pull request | an independent reader from another AI vendor when one is available, fix rounds, required checks, merges by the lead only | PW.2, PW.7, PS.1 |

## 9. How this maps to outside frameworks

**Alignment, not compliance.** This standard was written with the frameworks below open, and the tables show where it meets them and where it does not. It adopts none of them in full, nobody outside the team has assessed it, and nothing here may be described as compliant with or certified against any of them. Every page was read on 2026-10-28.

### 9.1 NIST Secure Software Development Framework (SSDF) 1.1

- **Version:** SSDF version 1.1, NIST SP 800-218, final, February 2022. NIST still lists it as final. Version 1.2 (NIST SP 800-218 Rev. 1) is an initial public draft of 17 December 2025; its comment period closed on 30 January 2026. The draft adds two practices: PO.6 (a plan for continuous improvement) and PS.4 (robust and reliable updates). We map to 1.1 and will map again when 1.2 is final.
- **Sources:** <https://csrc.nist.gov/Projects/ssdf/publications>, <https://csrc.nist.gov/pubs/sp/800/218/r1/ipd>, and the text of both documents.

| Practice | What agenttalk has | Gap |
|---|---|---|
| PO.1 Define security requirements | this standard; the trust model in SECURITY.md | the standard is new and untried |
| PO.2 Roles and responsibilities | owner (the lead), builder, reader (ROLES-SKILLS-PROCEDURES.md) | no training beyond the skills and lessons |
| PO.3 Supporting toolchains | the development gate and CI; actions pinned by commit | **gap:** the development tools are not audited for published vulnerabilities (section 8, pip-audit) |
| PO.4 Criteria for security checks | required checks green; each handoff line names a check that ran | criteria are per change, not one written list |
| PO.5 Secure development environments | zizmor; fork runs get no secrets; hand trials of agenttalk commands use a throwaway scratch store, and the tests run in a separate shell outside every store, inside a store fence that pytest sets up around its temporary folder ([CONTRIBUTING.md](../CONTRIBUTING.md#before-you-send-a-pull-request), since #573) | none known |
| PS.1 Protect code from tampering | the master ruleset (approval of the last push, required checks), merges by the lead only, gitleaks | none known |
| PS.2 Verify release integrity | releases are git tags installed from GitHub | **gap:** no signed release, published hash or build provenance |
| PS.3 Archive each release | git tags and GitHub releases | partial: no separate archive |
| PW.1 Design to meet security requirements | step 1, the challenge, design reads | none known |
| PW.2 Review the design | a design read by another seat | none known |
| PW.4 Reuse well-secured software | no runtime dependencies; S10; pip-audit on the runtime installation; Dependabot | none known for runtime packages |
| PW.5 Secure coding practices | step 2 and the ten principles; ruff `S`; semgrep project rules | the principles are untried |
| PW.6 Configure the compilation, interpreter and build processes | the gate starts the Python tools it runs as modules (pytest, ruff, bandit, pip-audit, and pip itself) through its own launcher, `isolated_tool_argv`, which uses isolated mode (`python -I`: Python ignores its environment variables and the user's own packages, and does not put the current folder first on the import path). semgrep, zizmor and gitleaks run as programs, without that launcher. The gate builds the sdist and the wheel, installs the wheel into a fresh environment without system packages and with pip's own configuration and cache turned off, then runs `pip check` and the tests against it; CI actions are pinned by commit | **gap:** no check that two builds of one commit give identical files; compiler and build settings for Java code are not covered |
| PW.7 Review and analyse code | the independent reader and the security pass; ruff, bandit, semgrep, CodeQL, gitleaks | none known |
| PW.8 Test executable code | the development gate's tests; the `test-security` skill; step 3's executed checks | none known |
| PW.9 Secure default settings | S7 (the dashboard binds the local machine; actions are off unless turned on) | none known |
| RV.1 Identify vulnerabilities | the private reporting route in SECURITY.md; weekly CodeQL and gate runs; Dependabot | none known |
| RV.2 Assess and fix vulnerabilities | issues and fix rounds | **gap:** no stated response time |
| RV.3 Find root causes | reviewers record each recurring class of mistake as a lesson in the team's knowledge store | the store is private to the team; outside readers see only the issues |

### 9.2 NIST SP 800-218A: secure development for generative AI

- **Version:** SP 800-218A, final, 26 July 2024. Source: <https://csrc.nist.gov/pubs/sp/800/218/a/final>.
- **Does it apply?** In part. Its scope is AI model development, "as well as incorporating and integrating AI models into other software". agenttalk trains no model, so the tasks about training data and model weights do not apply. It does run AI agents and a gateway to a paid model, which makes it software that uses generative AI models, one of the profile's audiences.
- **What we map:** section 4 follows its rule that human-written and AI-written code are treated alike; S1 treats model output as outside input.
- **Gaps:** the defence against instructions hidden in messages (prompt injection) lives in SECURITY.md (its prompt-injection scenario), not in this standard; the profile's tasks for systems that use models have not been mapped one by one.

### 9.3 OWASP Top 10 Proactive Controls, 2024 edition

- **Version:** the 2024 edition. On 2026-10-28 the project site shows it under "Top 10 2024" in its archive, and shows a next edition as "Future Top 10 (WIP)" with the same ten control names. Source: <https://top10proactive.owasp.org/>. The page says the document "is primarily written for developers": it is guidance, not a standard to comply with.

| Control | Principle | Gap |
|---|---|---|
| C1 Implement Access Control | S4 (changes, and reads, lists and exports of data that not everyone may see) | none known |
| C2 Use Cryptography to Protect Data | S6 (constant-time comparison, strong randomness, no home-made cryptography, secrets kept out of text) | **gap:** key management over a key's life (how keys are made, stored, rotated and revoked) is not covered |
| C3 Validate all Input & Handle Exceptions | S1, S7 | none known |
| C4 Address Security from the Start | step 1 | none known |
| C5 Secure By Default Configurations | S7 | none known |
| C6 Keep your Components Secure | S10 | none known |
| C7 Secure Digital Identities | S4, S6 in part | **gap:** agenttalk has no sign-in for people; its trust model is the local machine |
| C8 Leverage Browser Security Features | S5 in part | **gap:** browser security headers for the dashboard are not covered |
| C9 Implement Security Logging and Monitoring | S7 in part (a refusal says why) | **gap:** no principle for security logging |
| C10 Stop Server Side Request Forgery | S1 (outside input never chooses a network address unchecked) | none known |

### 9.4 OWASP Application Security Verification Standard (ASVS) 5.0.0

- **Version:** 5.0.0, released 30 May 2025. Sources: <https://asvs.owasp.org/> and the chapter files at <https://github.com/OWASP/ASVS/tree/master/5.0/en>.
- **How it is used:** ASVS sets requirements for web applications and web services, and invites each organisation to make its own tailored version, "omitting irrelevant sections". agenttalk is mostly a command-line tool, so only its two local web services are in scope: the dashboard and the paid gateway's front. We select chapters, claim no ASVS level (it has three), and have not yet selected requirements one by one.

| Chapter | Selected? | Where in agenttalk |
|---|---|---|
| V1 Encoding and Sanitization | yes | dashboard pages that show message text (S5) |
| V2 Validation and Business Logic | yes | every request (S1) |
| V3 Web Frontend Security | yes | dashboard pages |
| V4 API and Web Service | yes | the dashboard's data and action requests; the gateway front |
| V5 File Handling | yes | paths built from requests (S3) |
| V6 Authentication | no | no sign-in for people; the gateway front checks a shared token, which S4 and S6 cover; both services listen on the local machine only (gap, see C7) |
| V7 Session Management | no | no sessions |
| V8 Authorization | yes | dashboard actions and data; the gateway front's token check (S4) |
| V9 Self-contained Tokens | no | no self-contained tokens |
| V10 OAuth and OIDC | no | not used |
| V11 Cryptography | yes, in part | message signing, key comparison (S6); key management is not covered (see C2) |
| V12 Secure Communication | no | the services listen locally; **gap:** agenttalk's own outgoing network calls are not covered |
| V13 Configuration | yes | defaults and stored secrets (S6, S7) |
| V14 Data Protection | yes | private and client data (S6) |
| V15 Secure Coding and Architecture | yes | S2, S9, S10 |
| V16 Security Logging and Error Handling | yes, in part | S7 (see C9) |
| V17 WebRTC | no | not used |

### 9.5 CWE Top 25 Most Dangerous Software Weaknesses, 2025

- **Version:** the 2025 list. Source: <https://cwe.mitre.org/top25/> (page last updated 29 January 2026) and the list itself, <https://cwe.mitre.org/top25/archive/2025/2025_cwe_top25.html>.

| Principle | Weaknesses it addresses |
|---|---|
| S1 | CWE-20 improper input validation, CWE-918 server-side request forgery, CWE-434 unrestricted upload |
| S2 | CWE-78 OS command injection, CWE-77 command injection, CWE-89 SQL injection, CWE-94 code injection |
| S3 | CWE-22 path traversal |
| S4 | CWE-862 missing authorization, CWE-863 incorrect authorization, CWE-284 improper access control, CWE-639 authorization bypass through a user-controlled key, CWE-306 missing authentication for a critical function, CWE-352 cross-site request forgery (for the dashboard's actions) |
| S5 | CWE-79 cross-site scripting |
| S6 | CWE-200 exposure of sensitive information |
| S8 | CWE-770 allocation of resources without limits |
| S9 | CWE-502 deserialization of untrusted data |

**Gap:** seven entries are memory-safety errors (CWE-787, 416, 125, 120, 121, 122 and 476). Python and Java code cannot make the first six without native code, which agenttalk does not have; CWE-476 (a null dereference) in Java ends in an exception, which S7 says to handle. CWE-352 is named only through S4; no principle spells out request forgery for the dashboard.

### 9.6 SLSA (Supply-chain Levels for Software Artifacts) 1.2

- **Version:** 1.2, status "Approved". Source: <https://slsa.dev/spec/>.
- **Does it add an obligation?** No. SLSA is voluntary. agenttalk is installed from git tags on GitHub, and it publishes no built packages and no build provenance, so it meets no SLSA build level today. That is the same gap as SSDF PS.2. Version 1 adds nothing here; a signed or attested release is a separate decision.

### 9.7 EU Cyber Resilience Act (CRA)

- **Version:** Regulation (EU) 2024/2847. It entered into force on 10 December 2024; reporting obligations apply from 11 September 2026 and the main obligations from 11 December 2027. Sources: <https://digital-strategy.ec.europa.eu/en/policies/cyber-resilience-act>, <https://digital-strategy.ec.europa.eu/en/policies/cra-summary> and <https://digital-strategy.ec.europa.eu/en/policies/cra-open-source>.
- **What decides it:** the CRA covers products "made available on the market", that is, supplied in the course of a commercial activity. Free and open-source software that is published but not supplied that way is outside it, and so is a developer who contributes code to a project that is not under their responsibility. Charging a price is one sign of commercial activity, not the only one: the Commission's pages also count, for example, charging for support beyond its cost and using the software to make money from other services. Separately, a company that supports open-source software intended for commercial use can be an "open-source software steward", with lighter duties of its own (a cybersecurity policy, cooperation with the authorities, and reporting of actively exploited vulnerabilities).
- **The team's reading, on facts the maintainer confirmed:** on 2026-10-29 the maintainer confirmed that agenttalk has no paid support, that no money is made from it through other services, and that no company backs it as a steward. It is also free under the MIT licence, is not sold, and is installed from its public repository. On those facts, the team reads the CRA as not applying to agenttalk today. This is not legal advice. The answer would change if any of those facts changed; the [proposal to make the console sellable to clients](https://github.com/zoolok17/agenttalk/blob/master/docs/PROPOSAL-console-client-sellability.md) is such a case and would need this question answered first.

## 10. Owner, versions and review

- **Owner:** the lead. A change to the steps or the principles needs the same review as any other change, and makes a new version.
- **Wiring clarifications (2026-10-30).** When the standard was wired in, the pilot's four binding requirements were written into this text: the path example in section 6 now includes the real-path check; a handoff line takes one form; an `executed` line names only what its check covers; and the reader removes each protection to see its command fail. A note also names every refusal and its cost. They say what the existing rules already meant (a handoff line points at a check that ran, and the reader checks it) and add no new duty to a task, so the version stays `secure-coding@1`.
- **Versions:** version 1, 2026-10-28. A task names the version it works under (for example `secure-coding@1`), and work already started keeps that version when a new one appears. Changes made before the standard's first merge stay version 1, and the pilot uses the text as merged.
- **Review:** on 28 April 2027, or when NIST finalises its new SSDF version (1.2 is a draft today), whichever comes first, as decided on 2026-10-29. A review also comes earlier when OWASP publishes the next Proactive Controls, MITRE publishes a new CWE Top 25, a review finds a class of mistake the principles miss, or the facts behind section 9.7 change.
