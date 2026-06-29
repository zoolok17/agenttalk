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


@dataclass(frozen=True)
class Finding:
    """One currency problem in a bundled skill source file."""
    file: str
    line: int          # 1-based; 0 = file-level (frontmatter)
    token: str
    reason: str


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


def _command_spans(text: str) -> list[tuple[int, str]]:
    """Yield ``(line_no, command_tail)`` for every ``agenttalk`` / ``python -m agenttalk``
    invocation inside a FENCED code block or an inline-backtick span. Free prose is NOT
    scanned (conservative: avoids false positives on descriptive sentences).

    Inside a fence, SHELL LINE CONTINUATIONS are accumulated into one logical command before
    matching, so a stale flag/subcommand on a continuation line is still validated (the P1
    false-negative the reviewers caught - continuations are the dominant multi-line style):
    a line whose trailing non-space char is ``\\`` (bash) or `` ` `` (PowerShell) joins the
    next physical line; the reported line number is the FIRST physical line of the group.
    Both ``` and ~~~ toggle fences. (Known limitation: 4-space-indented code blocks are not
    scanned; no bundled skill uses them.) Honors ``ignore-next`` / ``ignore-line`` comments."""
    out: list[tuple[int, str]] = []
    in_fence = False
    ignore_next = False
    lines = text.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        if _IGNORE_LINE in raw:
            i += 1
            continue
        if _IGNORE_NEXT in raw:
            ignore_next = True
            i += 1
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            i += 1
            continue
        if ignore_next:
            ignore_next = False
            i += 1
            continue
        start_line = i + 1
        candidates: list[str] = []
        if in_fence:
            # accumulate shell continuations (trailing \ or `) into one logical line
            joined = raw
            while True:
                rs = joined.rstrip()
                if rs.endswith("\\") or rs.endswith("`"):
                    joined = rs[:-1]
                    if i + 1 < n:
                        i += 1
                        joined = joined + " " + lines[i]
                    else:
                        break
                else:
                    break
            candidates.append(joined)
        else:
            # prose line: only the inline-backtick spans are candidate commands
            candidates.extend(re.findall(r"`([^`]*)`", raw))
        for cand in candidates:
            m = _CMD_RE.search(cand)
            if m:
                out.append((start_line, m.group(1).strip()))
        i += 1
    return out


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

_DEVKIT_CATEGORIES = {"coordination", "production", "assurance", "reference"}


def check_skill_file(path: Path, *, kind: str, inventory: _Node,
                     current: tuple[int, int], require_stamp: bool = True) -> list[Finding]:
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
    if "description" not in keys:
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
                findings.append(Finding(rel, 0, "reviewed-against",
                                        f"reviewed-against {parsed[0]}.{parsed[1]} lags package "
                                        f"{current[0]}.{current[1]} (re-review against current CLI)"))

    # --- CLI-token lint ---
    for line_no, tail in _command_spans(text):
        for reason in _validate_command(tail, inventory):
            findings.append(Finding(rel, line_no, "agenttalk", reason))
    return findings


def check_bundled_skills(skills_root: Path, version: str) -> list[Finding]:
    """Lint every bundled skill source under ``skills_root``: claude/*.md, codex/*/SKILL.md,
    devkit/*/SKILL.md (skipping the _shared reference-holder's SKILL.md stamp requirement is
    handled by category=reference). Returns all findings. Degrade-safe per file."""
    inventory = build_command_inventory()
    current = current_major_minor(version)
    findings: list[Finding] = []

    for p in sorted((skills_root / "claude").glob("*.md")):
        findings.extend(check_skill_file(p, kind="claude", inventory=inventory, current=current))
    for p in sorted((skills_root / "codex").glob("*/SKILL.md")):
        findings.extend(check_skill_file(p, kind="codex", inventory=inventory, current=current))
    for p in sorted((skills_root / "devkit").glob("*/SKILL.md")):
        findings.extend(check_skill_file(p, kind="devkit", inventory=inventory, current=current))
    return findings
