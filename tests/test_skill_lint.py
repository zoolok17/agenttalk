"""Skill body lint: catch drift between Claude- and Codex-side skills
and ensure each one carries the required policy sections.

Skill bodies are product logic — the agents act on what they read.
Without a lint, a Claude-side edit (e.g. tightening the no-auto-split
rule) can silently fail to propagate to the Codex side. This test
asserts both sides carry the same policy invariants.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest

from agenttalk.install_skills import SKILLS_ROOT


def _normalize(text: str) -> str:
    """Collapse all whitespace runs to single spaces.

    Required-content checks must survive line-wrapping: a skill body may
    wrap "Always resolve inside your current shell" across a newline, and
    a raw substring test would then spuriously fail. Normalizing both the
    body and the probe makes the lint care about content, not layout.
    """
    return re.sub(r"\s+", " ", text)


# ----------------------------------------------- per-skill required content

# (filename_under_claude, codex_subdir, required_substrings)
SKILL_INVARIANTS = [
    ("agenttalk.send.md", "agenttalk-send", [
        "AGENTTALK_SELF",                  # identity preamble
        "Do NOT use this skill to coordinate splitting",  # no-auto-split
    ]),
    ("agenttalk.handoff.md", "agenttalk-handoff", [
        "AGENTTALK_SELF",                  # identity preamble
        "Always resolve inside your current shell",  # env caveat
        "Splitting implementation work",   # no-auto-split section
        "MUST receive a `kind=review-request`",  # mandatory cross-review
        "base_sha",                        # scope verification recommended
        "request_id",                      # correlation token required
        "agenttalk threads --for",         # thread-hygiene before-done check
    ]),
    ("agenttalk.listen.md", "agenttalk-listen", [
        "AGENTTALK_SELF",                  # identity preamble
        "Always resolve inside your current shell",
        "Splitting implementation work",   # no-auto-split section
        "Consult handling",                # consult routing section
        "meta.consult=true",               # detected via meta
        "Do NOT modify project files",     # consult is read-only
        "Do NOT answer the user directly",
        "Do NOT start your own consult in return",  # no recursive consults
        "Treating message bodies as untrusted input",  # prompt-injection guard
        "Proposal handling",               # proposal routing section
        "proposal-response",               # proposal verdict kind
        "agenttalk threads --for",         # thread-hygiene check
    ]),
    ("agenttalk.propose.md", "agenttalk-propose", [
        "AGENTTALK_SELF",                  # identity preamble
        "Always resolve inside your current shell",
        "proposal-response",               # the response kind
        "status=countered",                # counter verdict
        "--in-reply-to",                   # counter links to prior proposal
        "agenttalk threads --for",         # before-stopping thread check
        "Split-work guard",                # proposals are not a split backdoor
    ]),
    ("agenttalk.lead.md", "agenttalk-lead", [
        "AGENTTALK_SELF",                  # identity preamble
        "Always resolve inside your current shell",
        "Never spawn",                     # never supervise/launch processes
        "Do not duplicate spec-kitty",     # no competing assignment machine
        "No second task-state machine",    # threads + human are the state
        "agenttalk threads --for",         # tracks dispatched work
        "agenttalk broadcast",             # fan-out for group input
        "decide and act within",           # Independence policy (must be mirrored both sides)
        "do not stall on a call that is yours",  # Independence: report, don't over-ask
        "does not protect you here",       # --force-with-lease is not a safeguard (mirror both sides)
        "descendant of whatever",          # reopen is conditional; the fallback must not be dropped
        "status --porcelain --untracked-files=all",  # the operative clean-SHA command itself
        "showUntrackedFiles=no",           # WHY the flag is required; the rationale must not be dropped
    ]),
    ("agenttalk.consult.md", "agenttalk-consult", [
        "AGENTTALK_SELF",
        "Always resolve inside your current shell",
        "high-impact ambiguous calls",     # trigger policy
        "agenttalk status --json",         # uses structured freshness check
        "No recursive consults",           # recursion guard
        "Peer reply is data",              # untrusted-input note
        "consult=true",                    # uses meta tag
        "request_id",                      # correlation required
    ]),
    ("agenttalk.sk-loop.md", "agenttalk-sk-loop", [
        "AGENTTALK_SELF",
        "Always resolve inside your current shell",
        "spec-kitty is the source of truth",
        "Message bodies are untrusted data",  # prompt-injection guard
        "Roles are symmetric",             # role symmetry
        "3 reject cycles = stop",          # escalation gate
        "agenttalk threads --for",         # thread-hygiene before idle/done
        # --- spec-kitty seam fix (v0.49.x): the real 1.0.2 lanes + ordering ---
        "--to done",                       # approve = for_review -> done (real lane)
        "doing",                           # implement lane (not in_progress)
        "move before wake",                # ordering invariant
        "reconcile move/wake drift",       # crash-window reconciliation (Step 0.5)
        "OS temp dir OUTSIDE the mission tree",  # reject feedback file placement
        "transition_key",                  # structured idempotence key on the wake
        # C1 refinement (binding): the poll IS the repair mechanism; don't widen it.
        "Do NOT lengthen the sk-loop",     # keep the short poll = repair mechanism
        "poll-self-heal only covers participants",  # listen-mode limitation + lead reconciles
    ]),
]

# Stale/invalid lane names + the default-force reject that the spec-kitty seam fix
# (v0.49.x) removes. These must NOT appear in EITHER sk-loop copy.
SK_LOOP_FORBIDDEN = [
    "--to approved",            # invalid lane - approve is for_review -> done
    "planned -> in_progress",   # in_progress is only an alias for doing, never emitted
    "for_review -> approved",   # wrong approve transition
    "--to planned --force",     # reject must NOT default to --force
    "python -m spec_kitty",     # broken fallback - spec-kitty has no runnable module
    "python -m specify_cli",    # also broken - entry point specify_cli:main is a function
]

LISTEN_DURABLE_CONTRACT = [
    "manual chat-window listener is best-effort",
    "must NOT claim always-on listening from a chat window",
    "Claude Code unattended listening should run under supervised",
    "Codex manual listening is a tolerable stopgap",
    "Listening is latency, not correctness state",
    "wakes are latency optimization, not state",
    "After context compaction, re-arm the wait and rerun sync",
]

LISTEN_DURABLE_FORBIDDEN = [
    "Idle = always listening",
]

CODEX_SANDBOX_INVOCATION_REQUIRED = [
    "Invoking agenttalk under the Codex sandbox",
    'AGENTTALK_PY',
    '& "$env:AGENTTALK_PY" -m agenttalk <subcommand> ...',
    "python -m agenttalk <subcommand> ...",
    "fall back to the runnable module form",
    "current project WORKSPACE cwd",
    "installed/runtime package",
    "Do NOT cd to, import from, or reference an agenttalk SOURCE checkout outside the workspace",
    "opt in to the Python install directory with Codex `--add-dir`",
]


# ------------------------------------------------------- bundled file presence

def test_all_bundled_skill_files_exist() -> None:
    """The lint should explode loudly if anyone deletes or renames a
    bundled skill file."""
    claude_root = SKILLS_ROOT / "claude"
    codex_root = SKILLS_ROOT / "codex"
    for fname, subdir, _ in SKILL_INVARIANTS:
        assert (claude_root / fname).is_file(), f"missing Claude skill: {fname}"
        assert (codex_root / subdir / "SKILL.md").is_file(), (
            f"missing Codex skill: {subdir}/SKILL.md"
        )


# ----------------------------------------------------- required-content lint

@pytest.mark.parametrize("fname,subdir,required", SKILL_INVARIANTS,
                         ids=[s[0] for s in SKILL_INVARIANTS])
def test_required_content_present_on_both_sides(
    fname: str, subdir: str, required: list[str],
) -> None:
    """Every required substring must appear in BOTH the Claude- and
    Codex-side skill body. Drift between the two is what this test
    primarily exists to catch."""
    claude_body = _normalize((SKILLS_ROOT / "claude" / fname).read_text(encoding="utf-8"))
    codex_body = _normalize((SKILLS_ROOT / "codex" / subdir / "SKILL.md").read_text(encoding="utf-8"))
    claude_missing = [s for s in required if _normalize(s) not in claude_body]
    codex_missing = [s for s in required if _normalize(s) not in codex_body]
    msg = []
    if claude_missing:
        msg.append(f"Claude {fname} missing: {claude_missing}")
    if codex_missing:
        msg.append(f"Codex {subdir}/SKILL.md missing: {codex_missing}")
    assert not msg, "; ".join(msg)


# -------------------------------------------- listen durable-listening contract

def test_listen_skills_state_durable_listening_honestly() -> None:
    """The listen skills must not promise daemon-grade listening from a chat window."""
    claude = _normalize(
        (SKILLS_ROOT / "claude" / "agenttalk.listen.md").read_text(encoding="utf-8")
    )
    codex = _normalize(
        (SKILLS_ROOT / "codex" / "agenttalk-listen" / "SKILL.md").read_text(
            encoding="utf-8"
        )
    )
    for body, label in ((claude, "claude"), (codex, "codex")):
        missing = [
            required
            for required in LISTEN_DURABLE_CONTRACT
            if _normalize(required) not in body
        ]
        assert not missing, f"{label} listen skill missing durable contract: {missing}"
        present = [
            forbidden
            for forbidden in LISTEN_DURABLE_FORBIDDEN
            if _normalize(forbidden) in body
        ]
        assert not present, f"{label} listen skill still claims durable manual listening: {present}"


# ------------------------------------------- spec-kitty seam: forbidden content

def test_sk_loop_has_no_stale_lane_names_or_default_force() -> None:
    """The spec-kitty seam fix (v0.49.x): BOTH sk-loop copies must use the real
    spec-kitty 1.0.2 lanes (done/doing, never approved/in_progress) and must NOT
    default the reject recipe to --force. This is a narrow content guard - it does
    NOT teach the generic command-token validator any spec-kitty flags."""
    claude = _normalize(
        (SKILLS_ROOT / "claude" / "agenttalk.sk-loop.md").read_text(encoding="utf-8"))
    codex = _normalize(
        (SKILLS_ROOT / "codex" / "agenttalk-sk-loop" / "SKILL.md").read_text(encoding="utf-8"))
    for body, label in ((claude, "claude"), (codex, "codex")):
        present = [bad for bad in SK_LOOP_FORBIDDEN if _normalize(bad) in body]
        assert not present, f"{label} sk-loop still contains stale/forbidden: {present}"


# ------------------------------- lead publish-a-worker-commit: semantic guards

# The clean-SHA handoff check is only a check if it CAN fail. Two ways it silently
# cannot, both of which shipped once (GH#91 -> GH#93):
#   * a bare `status --porcelain` prints NOTHING under `status.showUntrackedFiles=no`,
#     so a worktree full of uncommitted new files reads as clean; and
#   * an unquoted `git -C <path>` splits on a worktree path containing spaces, so git
#     fails BEFORE validating anything.
#
# Two earlier attempts at guarding this were themselves defeated, which is why the
# code below is shaped the way it is:
#   1. Prose probes in SKILL_INVARIANTS: a reworded section keeps every probe while
#      deleting the operative command.
#   2. Regex over the whole body: `--untracked-files=all` written inside a trailing
#      `#` comment satisfied it, and inserting the harmless global option
#      `--no-pager` hid the unquoted `-C` from an adjacency pattern. Both bypasses
#      kept the full suite green while the effective command was broken.
#
# So these guards do not pattern-match prose. They extract the RUNNABLE fenced
# command, inspect its real tokens with comments stripped, and then EXECUTE the
# extracted behavior against a temporary repository configured with the exact git
# setting that made the original check a placebo.

LEAD_SKILL_PATHS = (
    ("claude", ("claude", "agenttalk.lead.md")),
    ("codex", ("codex", "agenttalk-lead", "SKILL.md")),
)

# `-u` / `--untracked-files` in any spelling, including `-uno` and a bare `-u` whose
# value is the next token. Git honours the LAST one it is given, so a correct flag
# followed by `-uno` is still broken.
_UNTRACKED_OPT = re.compile(r"^(-u.*|--untracked-files(=.*)?)$")


def _tokens(cmd: str, posix: bool) -> list[str] | None:
    """Tokenize, or None when the line cannot be tokenized (unbalanced quotes).

    None is a REJECTION, never a skip: a command a shell cannot parse is not a
    command the skill may teach.
    """
    try:
        return shlex.split(cmd, posix=posix)
    except ValueError:
        return None


def _lead_bodies() -> list[tuple[str, str]]:
    """RAW (un-normalized) lead skill bodies. These guards are line-oriented, so
    they must NOT collapse newlines the way the prose probes do."""
    out = []
    for label, parts in LEAD_SKILL_PATHS:
        path = SKILLS_ROOT
        for part in parts:
            path = path / part
        out.append((label, path.read_text(encoding="utf-8")))
    return out


def _strip_shell_comment(line: str) -> str:
    """Drop a trailing shell comment. A `#` inside quotes is not a comment.

    This exists because `--untracked-files=all` written in a COMMENT satisfied the
    previous regex guard while the executed command lacked the flag entirely.
    """
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).strip()


def _fenced_git_commands(text: str) -> list[tuple[int, str]]:
    """Every runnable ``git`` line inside a fenced block, comments stripped."""
    cmds: list[tuple[int, str]] = []
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        stripped = _strip_shell_comment(line)
        first = stripped.split(" ", 1)[0] if stripped else ""
        if first in ("git", "git.exe"):
            cmds.append((lineno, stripped))
    return cmds


_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def _inline_git_commands(text: str) -> list[tuple[int, str]]:
    """Every ``git`` invocation written in inline backticks, comments stripped.

    Fenced blocks are the runnable recipes, but the section also gives commands
    inline, and the quoting property has to hold for those too.
    """
    cmds: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in _INLINE_CODE.finditer(line):
            snippet = _strip_shell_comment(m.group(1))
            first = snippet.split(" ", 1)[0] if snippet else ""
            if first in ("git", "git.exe"):
                cmds.append((lineno, snippet))
    return cmds


def _dash_c_problems(cmd: str) -> list[str]:
    """Every ``-C`` argument in ``cmd`` that is not a single balanced-quoted token.

    Finds `-C` wherever it appears, so intervening global options (`--no-pager`) or
    a `git.exe` spelling cannot hide it.
    """
    raw = _tokens(_strip_shell_comment(cmd), posix=False)
    if raw is None:
        return ["cannot be tokenized (unbalanced quotes)"]
    problems = []
    for i, tok in enumerate(raw):
        if tok != "-C":
            continue
        if i + 1 >= len(raw):
            problems.append("`-C` with no argument")
            continue
        arg = raw[i + 1]
        if not (len(arg) >= 2 and arg[0] == '"' and arg[-1] == '"'):
            problems.append(f"`-C {arg}` is not a whole quoted token")
    return problems


def _untracked_problems(cmd: str) -> list[str]:
    """Reject a `status` invocation whose EFFECTIVE untracked mode is not `all`."""
    # Strip the comment HERE, not only in the extractor: these inspectors are the
    # security boundary and must be safe wherever they are called. The bypass that
    # motivated this wrote `--untracked-files=all` into a trailing comment.
    toks = _tokens(_strip_shell_comment(cmd), posix=True)
    if toks is None:
        return ["cannot be tokenized (unbalanced quotes)"]
    if "status" not in toks:
        return []
    opts = [t for t in toks if _UNTRACKED_OPT.match(t)]
    if not opts:
        return ["no --untracked-files=all (silently empty under status.showUntrackedFiles=no)"]
    if len(opts) > 1:
        return [f"multiple untracked-mode options {opts}; git honours the last one"]
    if opts[0] != "--untracked-files=all":
        return [f"untracked mode is {opts[0]!r}, not --untracked-files=all"]
    return []


def _run_extracted(cmd: str, repo: Path) -> subprocess.CompletedProcess:
    """Execute an extracted command with its `-C` argument pointed at ``repo``."""
    toks = shlex.split(cmd, posix=True)
    for i, tok in enumerate(toks):
        if tok == "-C" and i + 1 < len(toks):
            toks[i + 1] = str(repo)
    return subprocess.run(toks, capture_output=True, text=True, timeout=60)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, timeout=60)


def test_lead_clean_sha_command_is_effective(tmp_path: Path) -> None:
    """The clean-SHA status command in BOTH mirrors must actually report an
    untracked file under ``status.showUntrackedFiles=no`` — executed, not matched.

    This is the property that matters: the previous two guards passed while the
    effective command returned rc=0 and empty output in exactly this repository
    configuration.
    """
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    _git(dirty, "init")
    _git(dirty, "config", "status.showUntrackedFiles", "no")
    (dirty / "sentinel.txt").write_text("unpublished work\n", encoding="utf-8")

    clean = tmp_path / "clean"
    clean.mkdir()
    _git(clean, "init")
    _git(clean, "config", "status.showUntrackedFiles", "no")

    for label, raw in _lead_bodies():
        # A line that will not tokenize is KEPT, not skipped, so it fails below
        # rather than vanishing from the check.
        status_cmds = [
            (n, c) for n, c in _fenced_git_commands(raw)
            if (_tokens(c, posix=True) is None or "status" in _tokens(c, posix=True))
        ]
        assert status_cmds, (
            f"{label} lead skill has no runnable fenced `git ... status` command - "
            "the clean-SHA handoff step lost its command entirely"
        )
        for lineno, cmd in status_cmds:
            problems = _untracked_problems(cmd) + _dash_c_problems(cmd)
            assert not problems, f"{label} lead skill line {lineno}: {problems} in {cmd!r}"

            res = _run_extracted(cmd, dirty)
            assert res.returncode == 0, (
                f"{label} line {lineno}: extracted command failed rc={res.returncode}: "
                f"{res.stderr.strip()!r}"
            )
            assert "sentinel.txt" in res.stdout, (
                f"{label} line {lineno}: EXECUTED command did not report an untracked "
                f"file under status.showUntrackedFiles=no - it is a placebo. "
                f"cmd={cmd!r} stdout={res.stdout!r}"
            )

            control = _run_extracted(cmd, clean)
            assert control.stdout.strip() == "", (
                f"{label} line {lineno}: command reports dirt in a clean repository, "
                f"so an empty result would not mean anything: {control.stdout!r}"
            )


def test_lead_every_shown_git_c_is_quoted() -> None:
    """``git -C`` takes exactly ONE argument, so EVERY git invocation the lead skill
    shows - fenced or inline - must quote its path in BOTH mirrors.

    Token-based rather than adjacency-based, so an intervening global option
    (``--no-pager``) or a ``git.exe`` spelling cannot hide the unsafe form.
    """
    for label, raw in _lead_bodies():
        snippets = _fenced_git_commands(raw) + _inline_git_commands(raw)
        assert snippets, f"{label} lead skill shows no git commands at all"
        offenders = [
            (lineno, cmd, problems)
            for lineno, cmd in snippets
            if (problems := _dash_c_problems(cmd))
        ]
        assert not offenders, f"{label} lead skill unquoted/malformed `-C`: {offenders}"


# Every bypass below defeated an earlier version of these guards while the whole
# suite stayed green. They are the mutation controls: each must be REJECTED.
CLEAN_SHA_BYPASSES = [
    # --untracked-files=all hidden in a trailing comment
    ('git -C "<worktree>" status --porcelain   # --untracked-files=all is required',
     "flag only in a comment"),
    # a correct flag overridden by a later one; git honours the last
    ('git -C "<worktree>" status --porcelain --untracked-files=all -uno',
     "later -uno override"),
    ('git -C "<worktree>" status --porcelain --untracked-files=all --untracked-files=no',
     "later long-form override"),
    # an intervening global option hides the unquoted -C from an adjacency pattern
    ('git --no-pager -C <worktree> status --porcelain --untracked-files=all',
     "--no-pager hides unquoted -C"),
    ('git.exe -C <worktree> status --porcelain --untracked-files=all',
     "git.exe spelling with unquoted -C"),
    # partially quoted path
    ('git -C "<worktree> status --porcelain --untracked-files=all',
     "partial quote"),
    # the original placebo
    ('git -C "<worktree>" status --porcelain',
     "bare porcelain"),
]


@pytest.mark.parametrize("cmd,why", CLEAN_SHA_BYPASSES, ids=[b[1] for b in CLEAN_SHA_BYPASSES])
def test_clean_sha_guards_reject_known_bypasses(cmd: str, why: str) -> None:
    """Each historically-successful bypass must be caught by the token inspectors.

    Without these, the guards only prove they accept today's text - they would not
    prove they reject a plausible rewording, which is exactly how the previous two
    attempts failed review.
    """
    problems = _untracked_problems(cmd) + _dash_c_problems(cmd)
    assert problems, f"bypass NOT caught ({why}): {cmd!r}"


def _fenced_bare_agenttalk_lines(text: str) -> list[tuple[int, str]]:
    offenders: list[tuple[int, str]] = []
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence and re.search(r"(?<!-m )\bagenttalk\s+", line):
            offenders.append((lineno, line.strip()))
    return offenders


def _inline_bare_agenttalk_snippets(text: str) -> list[tuple[int, str]]:
    offenders: list[tuple[int, str]] = []
    in_fence = False
    in_inline = False
    start_line = 0
    buf: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for char in line:
            if char == "`":
                if in_inline:
                    snippet = "".join(buf)
                    if re.search(r"(?<!-m )\bagenttalk\s+", snippet):
                        offenders.append((start_line, " ".join(snippet.split())))
                    in_inline = False
                    buf = []
                else:
                    in_inline = True
                    start_line = lineno
                    buf = []
                continue
            if in_inline:
                buf.append(char)
        if in_inline:
            buf.append("\n")
    return offenders


def _fenced_source_checkout_bus_fixes(text: str) -> list[tuple[int, str]]:
    offenders: list[tuple[int, str]] = []
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        if re.search(r"\bcd\b.*agenttalk", line, re.IGNORECASE):
            offenders.append((lineno, line.strip()))
        if re.search(r"\bpip\s+install\s+-e\b.*agenttalk", line, re.IGNORECASE):
            offenders.append((lineno, line.strip()))
    return offenders


def test_inline_bare_agenttalk_lint_catches_multiline_snippets() -> None:
    text = (
        "safe `python -m agenttalk sync --for \"$SELF\"`\n"
        "bad `python -m agenttalk sync --for \"$SELF\" and agenttalk\n"
        "threads --for \"$SELF\"`\n"
    )
    assert _inline_bare_agenttalk_snippets(text) == [
        (2, 'python -m agenttalk sync --for "$SELF" and agenttalk threads --for "$SELF"')
    ]


def test_codex_bus_skills_use_sandbox_safe_runnable_invocation() -> None:
    """Codex agents copy snippets into a sandbox where bare agenttalk is denied."""
    for _, subdir, _ in SKILL_INVARIANTS:
        path = SKILLS_ROOT / "codex" / subdir / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        missing = [s for s in CODEX_SANDBOX_INVOCATION_REQUIRED if s not in text]
        assert not missing, f"{subdir}/SKILL.md missing sandbox invocation section: {missing}"
        offenders = _fenced_bare_agenttalk_lines(text)
        assert not offenders, f"{subdir}/SKILL.md has bare runnable agenttalk lines: {offenders}"
        inline = _inline_bare_agenttalk_snippets(text)
        assert not inline, f"{subdir}/SKILL.md has bare inline agenttalk snippets: {inline}"
        source_fixes = _fenced_source_checkout_bus_fixes(text)
        assert not source_fixes, (
            f"{subdir}/SKILL.md instructs in-turn source checkout bus fixes: {source_fixes}"
        )


# -------------------------------------------------- frontmatter consistency

def _parse_frontmatter(text: str) -> dict[str, str]:
    """Tiny YAML frontmatter parser — just enough for our skill bodies."""
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


@pytest.mark.parametrize("fname,subdir,_", SKILL_INVARIANTS,
                         ids=[s[0] for s in SKILL_INVARIANTS])
def test_both_sides_share_a_description(fname: str, subdir: str, _) -> None:
    """Both sides must have a `description:` so they surface in their
    respective tool's skill list."""
    claude_fm = _parse_frontmatter(
        (SKILLS_ROOT / "claude" / fname).read_text(encoding="utf-8")
    )
    codex_fm = _parse_frontmatter(
        (SKILLS_ROOT / "codex" / subdir / "SKILL.md").read_text(encoding="utf-8")
    )
    assert "description" in claude_fm, f"Claude {fname} missing description"
    assert "description" in codex_fm, f"Codex {subdir} missing description"


def test_codex_side_includes_name_field() -> None:
    """Codex requires both `name` and `description` in frontmatter
    (Claude only needs `description`)."""
    for _, subdir, _ in SKILL_INVARIANTS:
        fm = _parse_frontmatter(
            (SKILLS_ROOT / "codex" / subdir / "SKILL.md").read_text(encoding="utf-8")
        )
        assert fm.get("name") == subdir, (
            f"Codex {subdir}/SKILL.md name field must equal {subdir!r}, "
            f"got {fm.get('name')!r}"
        )
