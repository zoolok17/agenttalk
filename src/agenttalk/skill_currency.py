"""Mechanical skill-currency lint (Tier 0, skill-devkit evolution).

The existing doctor checks (``_check_skills`` / ``_check_devkit``) prove the INSTALLED
skill files match the BUNDLED source. They cannot prove the bundled SOURCE PROSE is
current: both copies can be byte-identical and equally stale (the exact gap the v0.42.0
lead-loop/relay refresh exposed). This module adds a DETERMINISTIC source-content lint:

  * CLI-token lint - every ``agenttalk`` / ``python -m agenttalk`` command referenced in a
    skill's fenced code blocks / inline-backtick spans must resolve against the LIVE
    argparse surface (``cli.build_parser()`` introspected recursively), so a renamed or
    removed command/flag is caught before the stale skill ships.
  * reviewed-against ratchet - a skill stamped older than the current package major/minor
    warns (patch-lag does not), forcing periodic re-read.
  * frontmatter well-formedness - required fields present (name/description/reviewed-against
    + category/evidence-profile for devkit).
  * pinned-runtime rule - a plain ``python -m agenttalk`` (``python``/``python3``/``py``, any
    spacing, continued lines joined) is flagged in a Claude skill (whose own form is the bare
    ``agenttalk``), and in a Codex or devkit skill unless that skill states the rule itself: use
    ``"$AGENTTALK_PY" -m agenttalk`` when ``AGENTTALK_PY`` is set, otherwise the project's form.

It is MECHANICAL: it proves referenced commands/flags EXIST and metadata is present. It does
NOT prove the surrounding prose is semantically correct (that stays a human review job). The
doctor surface is WARN-only; CI treats bundled-source regressions as failures (see the
source-tree test). Tier 0a covers frontmatter + ratchet + CLI-token lint; the evidence-stub
PARITY check lands in Tier 0b with ``_shared/references/evidence.md``.
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

# Tokens that are obviously placeholders, not real CLI words - never flagged.
_PLACEHOLDER_RE = re.compile(r"^(<.*>|REPLACE:.*|[A-Z][A-Z0-9_]{1,}|\.{3}|\$\{.*\}|%.*%)$")
_IGNORE_NEXT = "agenttalk-skill-lint: ignore-next"
_IGNORE_LINE = "agenttalk-skill-lint: ignore-line"
# The plain module form in any spelling of the interpreter and the spaces (`python`, `python3`, `python3.14`,
# `python.exe`, `py`, `py.exe`, a quoted name, a path ending in the name; also `python -m agenttalk.assurance`), with
# flags that take no value between the interpreter and `-m` (`py -3.14`, `python -I`). A shell continuation is joined
# before this is applied, so it is one command here. Known limit: a flag WITH a separate value (`python -X utf8 -m
# agenttalk`, `python -W ignore -m agenttalk`) and an interpreter under another name are not recognised.
_PLAIN_PYTHON_RE = re.compile(
    r"""(?:^|[\s(;&|"'\\/])"""
    r"""(?:python[\d.]*(?:\.exe)?|py(?:\.exe)?)["']?"""
    r"""(?:\s+-(?!m\s)[\w.-]+)*"""
    r"""\s+-m\s+agenttalk(?![\w-])"""
)
_PLAIN_PYTHON_FORM = "python -m agenttalk"
# A rule needs the condition, a Bash or PowerShell invocation, then the fallback nearby.
# Reject negation between the condition and invocation; require the invocation before the fallback. What follows the
# invocation, up to the fallback, is read by `_forbids_the_pinned_command` (a guide that says the pinned command "must
# not be used" states the opposite of the rule, but a "do not" about another command, or "not optional", does not).
# PowerShell accepts both quoted and unquoted executable variables after its call operator.
# Conditions may wrap at a line end or quote the name in backticks; bare mentions and
# HTML comments do not state the rule.
_FALLBACK = r"\botherwise\b|\bis\s+not\s+set\b|\bfall(?:s)?\s*back\b"
_NEGATION = r"\b(?:not|never|avoid|without|refrain|cannot|no|don['’]t|mustn['’]t|shouldn['’]t|can['’]t)\b"
_UP_TO_FALLBACK_WITHOUT_NEGATION = rf"(?:(?!{_FALLBACK}|{_NEGATION})[\s\S]){{0,600}}?"
_RULE_RE = re.compile(
    r"\b(?:if|when)\b[^.]{0,60}?(?<![\w])AGENTTALK_PY(?![\w])`?\s+is\s+set"
    + _UP_TO_FALLBACK_WITHOUT_NEGATION
    + r'(?:"\$AGENTTALK_PY"|&\s+(?:"\$env:AGENTTALK_PY"|\$env:AGENTTALK_PY))\s+-m\s+agenttalk(?![\w-])'
    + r"(?P<after>[\s\S]{0,600}?)"
    + rf"(?:{_FALLBACK})",
    re.IGNORECASE,
)
# A negation that supports the rule ("not optional", "never skip it") is not a prohibition of the pinned command.
_REINFORCEMENT_RE = re.compile(
    r"\b(?:not|never)\s+(?:to\s+)?(?:be\s+)?(?:optional|negotiable|skipp?(?:ed)?|omitt?(?:ed)?|ignor(?:ed?)|"
    r"replac(?:ed?)|bypass(?:ed)?|dropp?(?:ed)?|left\s+out)\b",
    re.IGNORECASE,
)
# In a clause after the one that holds the command, a negation counts only when it points back at the pinned command:
# "do not use it", "it must not be used", "do not use the pinned command" (but not "do not use the unpinned form").
_POINTS_BACK_RE = re.compile(
    rf"{_NEGATION}[^;.:!?]{{0,20}}?\b(?:use|used|run|call|invoke|follow|permit\w*|allow\w*)\b"
    r"\s+(?:it|this|that|them)\b"
    rf"|\b(?:it|this|that)\s+(?:\w+\s+){{0,2}}?{_NEGATION}"
    rf"|{_NEGATION}[^;.:!?]*\bpinned\b",
    re.IGNORECASE,
)
# A clause ends at ; ! ? a blank line, any colon except the two below, or a period before whitespace or the end of the
# text, closing quotes and brackets allowed between a period and that whitespace ("... is required.) Do not ...").
# A colon is exempt only as a drive-letter colon (one letter, the colon, then \ or /: `C:\temp`, `C:/temp`) or a URL
# scheme colon (followed by //), and a period only inside a word (`agenttalk.assurance`, `reply.md`, 0.105.0), written
# without backticks. Any other colon, one followed by `**` or a letter included, still ends the clause.
_CLAUSE_END_RE = re.compile(
    "[;!?]"
    "|:(?![\\\\/])"  # a colon not followed by a slash
    "|(?<![A-Za-z]):(?=[\\\\/])"  # a colon and slash that follow no letter
    "|(?<=[A-Za-z]{2}):(?=[\\\\/])(?!//)"  # after two letters (not a drive letter), unless it is a URL scheme
    "|\\.[)\\]}\"'\u2019\u201d]*(?!\\S)"
    "|\\n\\s*\\n"
)
_INLINE_CODE_RE = re.compile(r"(?<!`)`(?!`)[^`\n]+`(?!`)")
_CLAUSE_PUNCTUATION_RE = re.compile(r"[;.:!?]")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass(frozen=True)
class Finding:
    """One currency problem in a bundled skill source file.

    ``level`` (v0.44.0) splits BLOCKING mechanical drift from ADVISORY signals: ``error`` is
    real drift that must red the source-tree gate (missing/malformed stamp, dead CLI token,
    stub/parity drift, dangling continuation, frontmatter failure); ``warn`` is advisory and
    must NOT red CI (a parseable reviewed-against version LAG - a review signal, not
    machine-proven prose drift). The source-tree gate asserts no ``error``; doctor surfaces
    both at WARN severity."""
    file: str
    line: int          # 1-based; 0 = file-level (frontmatter)
    token: str
    reason: str
    level: str = "error"


def blocking_findings(findings: list[Finding]) -> list[Finding]:
    """The ``error``-level (CI-blocking) subset - real mechanical drift."""
    return [f for f in findings if f.level == "error"]


def warning_findings(findings: list[Finding]) -> list[Finding]:
    """The ``warn``-level (advisory) subset - e.g. a reviewed-against version lag."""
    return [f for f in findings if f.level == "warn"]


# --------------------------------------------------------------------------- inventory

@dataclass(frozen=True)
class _Node:
    """A resolved argparse parser node: its flags (option -> takes-a-value) and the
    subcommand names reachable from it."""
    flags: dict          # option_string -> takes_value(bool)
    subcommands: dict     # name -> _Node


def _node_from_parser(parser: argparse.ArgumentParser) -> _Node:
    flags: dict[str, bool] = {}
    subcommands: dict[str, _Node] = {}
    for action in parser._actions:  # noqa: SLF001 - argparse exposes structure only here
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            # choices maps subcommand-name -> its ArgumentParser; recurse (handles the
            # two-level subcommands: relay operator-answer, lane assign, knowledge publish...).
            for name, subparser in action.choices.items():
                subcommands[name] = _node_from_parser(subparser)
            continue
        for opt in action.option_strings:
            # nargs == 0 (store_true/store_const/count/help/version) takes NO value;
            # anything else consumes at least one following token as its value.
            flags[opt] = action.nargs != 0
    return _Node(flags=flags, subcommands=subcommands)


def build_command_inventory() -> _Node:
    """Resolve the live agenttalk command surface from ``cli.build_parser()`` (NOT --help
    text). Returns the root :class:`_Node`; its ``flags`` are the GLOBAL options."""
    from agenttalk.cli import build_parser
    return _node_from_parser(build_parser())


# --------------------------------------------------------------------------- scanning

# A real CLI invocation: `agenttalk` (optionally `python -m agenttalk`) preceded by
# start-or-whitespace and FOLLOWED BY whitespace, then the command tail to end-of-line.
# The space-after requirement excludes dotted skill names (`agenttalk.consult`) and the
# start-or-space-before requirement excludes slash-command references (`/agenttalk.lead`) -
# those are Claude/Codex skill invocations, NOT agenttalk CLI commands (false-positive class
# found in the first smoke run).
_CMD_RE = re.compile(r"(?:^|\s)(?:python\s+-m\s+)?agenttalk(?=\s)(.*)$")


def _is_fence(line: str) -> bool:
    s = line.strip()
    return s.startswith("```") or s.startswith("~~~")


def _is_boundary_line(line: str) -> bool:
    """A non-content line a shell continuation must NOT cross: a fence delimiter, a blank
    line, or a line carrying an ignore marker. ONE definition, used by BOTH the outer scan
    loop's special-casing AND the inner continuation peek, so the two boundary sets can never
    diverge - that divergence is exactly what produced the fence edge (fold 2) and the
    blank/ignore edge (fold 3). Adding a future boundary type updates only this predicate."""
    return (_is_fence(line) or not line.strip()
            or _IGNORE_LINE in line or _IGNORE_NEXT in line)


def _command_spans(text: str) -> tuple[list[tuple[int, str]], list[int]]:
    """Return ``(spans, dangling_lines)``.

    ``spans`` is ``(line_no, command_tail)`` for every ``agenttalk`` / ``python -m agenttalk``
    invocation inside a FENCED code block or an inline-backtick span. Free prose is NOT
    scanned (conservative). Inside a fence, SHELL LINE CONTINUATIONS are accumulated into one
    logical command before matching, so a stale flag/subcommand on a continuation line is
    still validated (the dominant multi-line style): a line whose trailing non-space char is
    ``\\`` (bash) or `` ` `` (PowerShell) joins the next physical line; the reported line is
    the FIRST physical line of the group. Both ``` and ~~~ toggle fences. (Limitation:
    4-space-indented code blocks are not scanned; no bundled skill uses them.)

    ``dangling_lines`` is the start line of any command whose continuation runs into a FENCE
    DELIMITER or EOF instead of a real next line - a malformed multi-line command. The
    accumulator MUST NOT consume the delimiter (else it eats the closing fence as command
    text, the closing backticks read as another continuation, the fence never toggles, and a
    LATER fenced command is hidden - the cascade both delta reviewers caught). It stops, flags
    the dangling line, and leaves the delimiter for the outer loop to toggle the fence.
    Honors ``ignore-next`` / ``ignore-line`` comments."""
    candidates, dangling = _scan_candidates(text)
    spans: list[tuple[int, str]] = []
    for start_line, cand in candidates:
        m = _CMD_RE.search(cand)
        if m:
            spans.append((start_line, m.group(1).strip()))
    return spans, dangling


def _scan_candidates(text: str, *, prose: bool = False) -> tuple[list[tuple[int, str]], list[int]]:
    """The scanner behind ``_command_spans``: ``(candidates, dangling_lines)`` where a candidate is
    ``(first_line_no, text)``: a fenced command with its shell continuations joined, or an inline-backtick span (and,
    with ``prose``, the whole prose line as well). Fences, continuations, boundaries and the ignore comments are decided
    here, once, so every lint that reads commands out of a skill sees the same thing."""
    candidates_found: list[tuple[int, str]] = []
    dangling: list[int] = []
    in_fence = False
    ignore_next = False
    lines = text.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        raw = lines[i]
        if _is_boundary_line(raw):
            # a non-content line: apply its specific effect, never scan it for a command
            if _is_fence(raw):
                in_fence = not in_fence
            elif _IGNORE_NEXT in raw:
                ignore_next = True
            # blank or ignore-line: skip with no effect
            i += 1
            continue
        if ignore_next:
            ignore_next = False
            i += 1
            continue
        start_line = i + 1
        candidates: list[str] = []
        if in_fence:
            joined = raw
            while True:
                rs = joined.rstrip()
                if not (rs.endswith("\\") or rs.endswith("`")):
                    break
                # peek the next physical line BEFORE consuming it: a continuation must not
                # cross ANY boundary (fence close, blank, ignore marker) or EOF - else it
                # eats the boundary as command text (cascading) and orphans the real
                # continuation token. Stop, flag the dangling start line, do NOT consume.
                if i + 1 >= n or _is_boundary_line(lines[i + 1]):
                    dangling.append(start_line)
                    joined = rs[:-1]
                    break
                joined = rs[:-1]
                i += 1
                joined = joined + " " + lines[i]
            candidates.append(joined)
        else:
            candidates.extend(re.findall(r"`([^`]*)`", raw))
            if prose:
                candidates.append(raw)
        candidates_found.extend((start_line, cand) for cand in candidates)
        i += 1
    return candidates_found, dangling


def _is_placeholder(tok: str) -> bool:
    return bool(_PLACEHOLDER_RE.match(tok))


def _validate_command(tail: str, root: _Node) -> list[str]:
    """Validate one ``agenttalk <...>`` invocation (the part AFTER ``agenttalk``) against the
    inventory. Returns a list of human-readable reasons (empty = clean). Conservative: only
    flags a token when it is confidently wrong (an unknown flag at the resolved node, or an
    unknown FIRST subcommand of a node that has subcommands). Stops at a literal ``--``
    (wrapper passthrough); skips placeholders; consumes known value-flags' values."""
    try:
        toks = tail.split()
    except Exception:  # noqa: BLE001
        return []
    reasons: list[str] = []
    node = root
    leaf = False                # reached a leaf positional / gave up sub-resolution
    expecting_value = False     # previous token was a value-taking flag
    for tok in toks:
        if tok == "--":
            break               # everything after is the wrapped CLI's argv, not ours
        if tok.startswith("#"):
            break               # a trailing shell comment - rest of the line is not args
        if expecting_value:
            expecting_value = False
            # the prior value-taking flag's value - UNLESS this token is itself a flag, in
            # which case a stale flag must not be silently swallowed as a value: fall through
            # to validate it (P3). (`--` and `#` are already handled by the breaks above.)
            if not (tok.startswith("-") and tok != "-"):
                continue
        if _is_placeholder(tok):
            if node.subcommands:
                leaf = True     # a placeholder in subcommand position - stop sub-resolution
            continue
        if tok.startswith("-") and tok != "-":
            opt = tok.split("=", 1)[0]
            # Global flags (--root/--version/-h) are accepted at ANY position - skills show
            # them before the subcommand, and accepting them anywhere avoids false positives
            # on arg-order; the lint's job is renamed/removed commands, not arg order.
            known = opt in node.flags or opt in root.flags
            if not known:
                reasons.append(f"unknown flag {opt!r} for this command")
                continue
            takes_value = node.flags.get(opt, root.flags.get(opt, False))
            if takes_value and "=" not in tok:
                expecting_value = True
            continue
        # a non-flag, non-placeholder token: descend through NESTED subcommands as deep as
        # they match (handles two-level: `roster add`, `relay operator-answer`, `close open`).
        if node.subcommands and not leaf:
            if tok in node.subcommands:
                node = node.subcommands[tok]
            else:
                reasons.append(f"unknown subcommand {tok!r}")
                leaf = True     # do not cascade further sub-errors on this command
        # else: a positional value at a leaf node - ignore
    return reasons


# --------------------------------------------------------------------------- frontmatter

def _frontmatter_keys(text: str) -> tuple[dict[str, str], bool]:
    """Return ``(top_level_keys, had_frontmatter)``. Lightweight: parses the leading
    ``---`` ... ``---`` block for indent-0 ``key:`` lines (value = inline remainder, or ''
    for block/list values). Avoids a YAML dependency; presence + a scalar value is all the
    currency checks need."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, False
    keys: dict[str, str] = {}
    for ln in lines[1:]:
        if ln.strip() == "---":
            return keys, True
        m = re.match(r"^([A-Za-z][\w-]*):(.*)$", ln)  # indent-0 key only
        if m:
            keys[m.group(1)] = m.group(2).strip()
    return keys, False   # unterminated frontmatter


def _frontmatter_profiles(text: str) -> list[str]:
    """Parse the frontmatter ``evidence-profile`` LIST (YAML block items, or an inline scalar)
    - the scalar :func:`_frontmatter_keys` cannot, since it is a list. Reads only within the
    leading ``---`` ... ``---`` block. Returns [] if absent."""
    profs: list[str] = []
    in_block = False
    in_ep = False
    for ln in text.splitlines():
        if ln.strip() == "---":
            if in_block:
                break
            in_block = True
            continue
        if not in_block:
            continue
        if re.match(r"^evidence-profile:", ln):
            inline = ln.split(":", 1)[1].strip()
            if inline:
                profs.append(inline)
            else:
                in_ep = True
            continue
        if in_ep:
            m = re.match(r"^\s+-\s+(\S+)", ln)
            if m:
                profs.append(m.group(1).strip())
            elif ln.strip() and not ln.startswith(" "):
                in_ep = False
    return profs


def parse_reviewed_against(value: str) -> tuple[int, int] | None:
    """Parse a ``reviewed-against`` stamp (optional leading ``v``, optional quotes) into a
    ``(major, minor)`` tuple, or None if unparseable."""
    s = value.strip().strip('"').strip("'").lstrip("vV")
    m = re.match(r"^(\d+)\.(\d+)", s)
    return (int(m.group(1)), int(m.group(2))) if m else None


def current_major_minor(version: str) -> tuple[int, int]:
    m = re.match(r"^(\d+)\.(\d+)", version)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


# --------------------------------------------------------------------------- per-file

def _join_fenced_continuations(text: str) -> str:
    """``text`` with every shell continuation inside a fenced block joined into one line, as the command scan joins it
    (a trailing ``\\`` for Bash or a trailing backtick for PowerShell, never across a boundary line), so a pinned
    command split over two lines reads as one. Prose is left as it is."""
    lines, joined_lines, in_fence, i = text.splitlines(), [], False, 0
    while i < len(lines):
        line = lines[i]
        if _is_fence(line):
            in_fence = not in_fence
        elif in_fence:
            while line.rstrip().endswith(("\\", "`")) and i + 1 < len(lines) and not _is_boundary_line(lines[i + 1]):
                i += 1
                line = line.rstrip()[:-1] + " " + lines[i]
        joined_lines.append(line)
        i += 1
    return "\n".join(joined_lines)


def _forbids_the_pinned_command(after: str) -> bool:
    """Whether the words after the pinned command, up to the rule's fallback, forbid that command ("... must not be
    used", "... is never permitted"). A negation in the command's own clause does; one in a later clause does only
    when it points back at the command; a negation about another command ("do not use the unpinned form") or one that
    supports the rule ("not optional") does not. Known limit: this reads wording, so a prohibition phrased without any
    of the listed negation words (`forbidden`) is not seen."""
    for number, clause in enumerate(_CLAUSE_END_RE.split(after)):
        clause = _REINFORCEMENT_RE.sub("", clause)
        if not re.search(_NEGATION, clause, re.IGNORECASE):
            continue
        if number == 0 or _POINTS_BACK_RE.search(clause):
            return True
    return False


def _mask_inline_code_punctuation(text: str) -> str:
    """``text`` with every clause-ending character inside a one-line inline code span replaced by a space, so the copy
    is as long as the text and every word stays where it was: the dot in `` `agenttalk.assurance` `` or the colon in
    `` `--meta status:done` `` does not end the clause the command is in. A span that is not closed on its line, and a
    double-backtick span, are left as they are."""
    return _INLINE_CODE_RE.sub(lambda span: _CLAUSE_PUNCTUATION_RE.sub(" ", span.group(0)), text)


def _states_pinned_rule(text: str) -> bool:
    """True when the skill states the pinned-runtime rule itself: a condition on ``AGENTTALK_PY`` being set, then the
    pinned command (not forbidden, and a command split over two lines counts), with what to do otherwise close by. A
    mention, the name inside an HTML comment or inside a longer name, or the condition without its fallback does not
    count."""
    joined = _join_fenced_continuations(_HTML_COMMENT_RE.sub("", text))
    masked = _mask_inline_code_punctuation(joined)  # read only to cut clauses; the pattern reads `joined`
    return any(
        not _forbids_the_pinned_command(masked[found.start("after"):found.end("after")])
        for found in _RULE_RE.finditer(joined)
    )


def _pinned_runtime_findings(rel: str, text: str, kind: str) -> list[Finding]:
    """The pinned-runtime rule. A Claude skill uses the bare ``agenttalk``, so a plain module form (``python``,
    ``python3`` or ``py`` with ``-m agenttalk``, any spacing, continued lines joined) is flagged on every line it
    appears. A Codex or devkit skill may use it only when it states the rule (``_states_pinned_rule``); otherwise one
    finding for the file, at the first such line. Fences, continuations and the ignore comments are read by the same
    scanner as the CLI-token lint. This checks what the skills SAY; it cannot make any agenttalk newer or prove which
    release a runtime is."""
    if kind not in {"claude", "codex", "devkit"}:
        return []
    candidates, _dangling = _scan_candidates(text, prose=True)
    hits = sorted({line for line, cand in candidates if _PLAIN_PYTHON_RE.search(cand)})
    if not hits:
        return []
    if kind == "claude":
        return [Finding(rel, n, _PLAIN_PYTHON_FORM,
                        "plain `python -m agenttalk` in a Claude skill: use the bare `agenttalk` form this skill "
                        "family uses (the seat's shim resolves its pinned runtime)") for n in hits]
    if _states_pinned_rule(text):
        return []
    return [Finding(rel, hits[0], _PLAIN_PYTHON_FORM,
                    "plain `python -m agenttalk` without the pinned-runtime rule: state it once, in a sentence: run "
                    'agenttalk commands as `"$AGENTTALK_PY" -m agenttalk` when AGENTTALK_PY is set, otherwise the form '
                    "your project's agent manual gives")]


_DEVKIT_CATEGORIES = {"coordination", "production", "assurance", "reference"}


def parse_evidence_profiles(text: str) -> dict[str, list[str]]:
    """Parse the canonical _shared/references/evidence.md into {profile_id: [field, ...]}.

    A profile is a ``## <id>`` heading; its required fields are the backtick-wrapped tokens of
    the bullet list under a ``Required fields:`` line, up to the next ``Rules:`` line or the
    next profile heading. The SAME parser feeds the stub-insertion and the stub-parity check,
    so the canonical list has exactly one source."""
    profiles: dict[str, list[str]] = {}
    cur: str | None = None
    in_fields = False
    for ln in text.splitlines():
        m = re.match(r"^##\s+([a-z][a-z0-9-]*)\s*$", ln)
        if m:
            cur = m.group(1)
            profiles[cur] = []
            in_fields = False
            continue
        if cur is None:
            continue
        low = ln.strip().lower()
        if low.startswith("required fields:"):
            in_fields = True
            continue
        if low.startswith("rules:"):
            in_fields = False
            continue
        if in_fields:
            fm = re.match(r"^-\s+`([A-Za-z_][A-Za-z0-9_]*)`", ln)
            if fm:
                profiles[cur].append(fm.group(1))
    return profiles


def _parse_skill_stub(text: str) -> tuple[str | None, list[str]]:
    """Parse a skill's in-skill evidence stub: returns ``(profile_id, [field, ...])`` from the
    ``## Evidence`` section (None / [] if absent). Mirrors the evidence.md field-bullet format
    so parity is a set comparison.

    Terminator convention (fresh P3): the field-bullet list runs until the first non-bullet,
    non-blank line or the next heading; :func:`parse_evidence_profiles` instead ends at the
    profile's ``Rules:`` line. The terminators differ but extract the SAME bullet set for the
    two file shapes, and any divergence is fail-safe - it can only produce a false-positive
    parity mismatch (a loud finding), never a silent equate. Keep field bullets contiguous."""
    profile: str | None = None
    fields: list[str] = []
    in_evidence = False
    in_fields = False
    for ln in text.splitlines():
        if re.match(r"^##\s+", ln):
            in_evidence = ln.strip().lower().startswith("## evidence")
            in_fields = False
            continue
        if not in_evidence:
            continue
        if profile is None:
            pm = re.search(r"`([a-z][a-z0-9-]*)`\s+profile", ln)
            if pm:
                profile = pm.group(1)
        if ln.strip().lower().startswith("required fields:"):
            in_fields = True
            continue
        if in_fields:
            fm = re.match(r"^-\s+`([A-Za-z_][A-Za-z0-9_]*)`", ln)
            if fm:
                fields.append(fm.group(1))
            elif ln.strip() and not ln.strip().startswith("-"):
                in_fields = False
    return profile, fields


def check_skill_file(path: Path, *, kind: str, inventory: _Node,
                     current: tuple[int, int], require_stamp: bool = True,
                     evidence_profiles: dict[str, list[str]] | None = None) -> list[Finding]:
    """Lint one bundled skill source file. ``kind`` is ``claude`` | ``codex`` | ``devkit``.
    Returns findings (empty = clean). Does NOT raise on read error - reports it as a finding."""
    rel = path.name if path.name != "SKILL.md" else f"{path.parent.name}/SKILL.md"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        # UnicodeDecodeError is a ValueError, NOT an OSError - catch it explicitly so an
        # undecodable file degrades to a Finding instead of crashing check_bundled_skills
        # (which the source-tree CI test calls unguarded) - the per-file degrade contract (P2).
        return [Finding(rel, 0, "<file>", f"unreadable: {e}")]

    findings: list[Finding] = []
    keys, closed = _frontmatter_keys(text)
    if not closed:
        # missing opening --- OR an unterminated block (opening --- + keys but no closing
        # ---): a loader may not parse it as frontmatter at all, so the stamp/category gate
        # would be silently void (codex MAJOR). Report it explicitly.
        findings.append(Finding(rel, 0, "frontmatter",
                                "missing or unterminated frontmatter block (--- ... ---)"))
    is_skill_md = path.name == "SKILL.md"
    is_devkit = kind == "devkit"
    category = keys.get("category", "")

    # --- frontmatter well-formedness ---
    if is_skill_md and "name" not in keys:
        findings.append(Finding(rel, 0, "name", "missing required frontmatter: name"))
    if kind != "reference" and "description" not in keys:
        # reference markdown (evidence.md / routing.md) carries only a reviewed-against stamp;
        # it is not an invocable skill, so name/description/category/evidence-profile do not apply.
        findings.append(Finding(rel, 0, "description", "missing required frontmatter: description"))
    if is_devkit:
        if "category" not in keys:
            findings.append(Finding(rel, 0, "category", "missing required devkit frontmatter: category"))
        elif category not in _DEVKIT_CATEGORIES:
            findings.append(Finding(rel, 0, "category",
                                    f"invalid category {category!r} (one of {sorted(_DEVKIT_CATEGORIES)})"))
        if category != "reference" and "evidence-profile" not in keys:
            findings.append(Finding(rel, 0, "evidence-profile",
                                    "missing required devkit frontmatter: evidence-profile"))
        # --- in-skill evidence-stub PARITY (Tier 0b) ---
        # A non-reference devkit skill must carry a small visible evidence stub (a loader may
        # show only SKILL.md, not the sibling reference), and the stub's profile + field set
        # must MATCH the canonical evidence.md profile - so the local stub can never drift from
        # the single source.
        if category != "reference" and evidence_profiles is not None:
            stub_profile, stub_fields = _parse_skill_stub(text)
            fm_profiles = _frontmatter_profiles(text)
            if stub_profile is None:
                findings.append(Finding(rel, 0, "evidence-stub",
                                        "missing in-skill evidence stub (## Evidence with the "
                                        "profile + required fields, linking ../_shared/references/evidence.md)"))
            else:
                # the stub profile must match the FRONTMATTER evidence-profile (else a skill can
                # declare one profile and stub another - reviewer-1 P1).
                if fm_profiles and stub_profile not in fm_profiles:
                    findings.append(Finding(rel, 0, "evidence-stub",
                                            f"in-skill stub profile {stub_profile!r} is not in the "
                                            f"frontmatter evidence-profile {fm_profiles}"))
                if stub_profile not in evidence_profiles:
                    findings.append(Finding(rel, 0, "evidence-stub",
                                            f"evidence stub names unknown profile {stub_profile!r} "
                                            f"(not in evidence.md)"))
                else:
                    canonical = evidence_profiles[stub_profile]
                    if set(stub_fields) != set(canonical):
                        missing = sorted(set(canonical) - set(stub_fields))
                        extra = sorted(set(stub_fields) - set(canonical))
                        findings.append(Finding(rel, 0, "evidence-stub",
                                                f"evidence stub fields differ from evidence.md "
                                                f"profile {stub_profile!r} (missing={missing}, extra={extra})"))

    # --- reviewed-against stamp + ratchet (bus + devkit; reference skills still stamp) ---
    if require_stamp:
        stamp = keys.get("reviewed-against", "")
        if "reviewed-against" not in keys:
            findings.append(Finding(rel, 0, "reviewed-against",
                                    "missing required frontmatter stamp: reviewed-against"))
        else:
            parsed = parse_reviewed_against(stamp)
            if parsed is None:
                findings.append(Finding(rel, 0, "reviewed-against",
                                        f"malformed reviewed-against stamp {stamp!r}"))
            elif parsed < current:
                # ADVISORY (v0.44.0): a parseable-but-old stamp is a review signal, not
                # machine-proven prose drift - it must NOT red the source-tree/CI gate (the
                # v0.43.0 failure mode, where a package bump alone made every skill lag).
                findings.append(Finding(rel, 0, "reviewed-against",
                                        f"reviewed-against {parsed[0]}.{parsed[1]} lags package "
                                        f"{current[0]}.{current[1]} (re-review against current CLI)",
                                        level="warn"))

    # --- CLI-token lint ---
    spans, dangling = _command_spans(text)
    for ln in dangling:
        findings.append(Finding(rel, ln, "agenttalk",
                                "dangling line-continuation (trailing \\ or `) at a boundary "
                                "(fence/blank/ignore) or EOF - malformed multi-line command"))
    for line_no, tail in spans:
        for reason in _validate_command(tail, inventory):
            findings.append(Finding(rel, line_no, "agenttalk", reason))
    findings.extend(_pinned_runtime_findings(rel, text, kind))
    return findings


def check_bundled_skills(skills_root: Path, version: str) -> list[Finding]:
    """Lint every bundled skill source under ``skills_root``: claude/*.md, codex/*/SKILL.md,
    devkit/*/SKILL.md (skipping the _shared reference-holder's SKILL.md stamp requirement is
    handled by category=reference). Returns all findings. Degrade-safe per file."""
    inventory = build_command_inventory()
    current = current_major_minor(version)
    findings: list[Finding] = []

    # parse the canonical evidence profiles once, for the in-skill stub-parity check
    evidence_profiles: dict[str, list[str]] = {}
    ev_path = skills_root / "devkit" / "_shared" / "references" / "evidence.md"
    if ev_path.exists():
        try:
            evidence_profiles = parse_evidence_profiles(ev_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            evidence_profiles = {}

    for p in sorted((skills_root / "claude").glob("*.md")):
        findings.extend(check_skill_file(p, kind="claude", inventory=inventory, current=current))
    for p in sorted((skills_root / "codex").glob("*/SKILL.md")):
        findings.extend(check_skill_file(p, kind="codex", inventory=inventory, current=current))
    for p in sorted((skills_root / "devkit").glob("*/SKILL.md")):
        findings.extend(check_skill_file(p, kind="devkit", inventory=inventory, current=current,
                                         evidence_profiles=evidence_profiles))
    # shared reference markdown (evidence.md / routing.md): reviewed-against ratchet + CLI-token
    # lint only - not skills, so no name/category/evidence-profile. Scope: _shared/references/.
    for p in sorted((skills_root / "devkit" / "_shared" / "references").glob("*.md")):
        findings.extend(check_skill_file(p, kind="reference", inventory=inventory, current=current))
    return findings
