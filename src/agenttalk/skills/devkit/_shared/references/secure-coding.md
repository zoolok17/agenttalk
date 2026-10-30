---
reviewed-against: "0.104"
---

# Secure coding: the general rules

These are the general rules for writing code with security in mind: a note before you write, protections while you write, and a handoff that points each choice at code and at a check that ran. The skills that write code point here in their first step. The rules name no project, so they apply in any project that installs these skills; a project may add its own boundaries and rules next to them.

Version: secure-coding@1

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
