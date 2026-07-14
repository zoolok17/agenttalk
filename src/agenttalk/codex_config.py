"""Manage the per-project block in ~/.codex/config.toml so Codex can call
agenttalk from inside its sandbox.

A "fully enabled" agenttalk project looks like:

    [projects.'<lowercase project dir>']
    trust_level = "trusted"
    approval_policy = "never"
    sandbox_mode = "workspace-write"

We do line-based editing (no external TOML dep) and match the project path
case-insensitively, since Codex accepts either casing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agenttalk._atomic import write_text as _atomic_write_text


def _read_text_no_translate(path: Path) -> str:
    """Read *path* as UTF-8 with universal-newline translation disabled.

    ``Path.read_text(newline=...)`` only gained the ``newline`` keyword in
    Python 3.13; on 3.10-3.12 it raises ``TypeError``. ``open(newline="")`` has
    accepted it for far longer, so go through it directly to stay portable.
    """
    with open(path, encoding="utf-8-sig", newline="") as f:
        return f.read()


MANAGED_KEYS = ("trust_level", "approval_policy", "sandbox_mode")
TARGET_VALUES = {
    "trust_level": '"trusted"',
    "approval_policy": '"never"',
    "sandbox_mode": '"workspace-write"',
}


def default_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _toml_quote_key(s: str) -> str:
    """Render a string as a TOML table-key quoted segment.

    Prefer a literal string (single-quoted) when safe, since Windows
    paths look cleanest unescaped. Fall back to a basic string
    (double-quoted) when the value contains a single quote or any
    control character that's illegal in literal strings.
    """
    # TOML literal strings forbid single quotes, newlines, and most
    # control characters. If the path is "clean", use the literal form.
    if "'" not in s and not any(c in s for c in "\r\n\t\b\f\x00"):
        return f"'{s}'"
    # Otherwise emit a basic string with TOML escapes.
    escaped = (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\b", "\\b")
         .replace("\t", "\\t")
         .replace("\n", "\\n")
         .replace("\f", "\\f")
         .replace("\r", "\\r")
    )
    return f'"{escaped}"'


@dataclass
class Result:
    action: str  # "created" | "updated" | "unchanged" | "removed" | "no-op"
    config_path: Path
    project_dir: str
    changes: list[str] = field(default_factory=list)


def _normalize_path(p: Path) -> str:
    """Codex stores project keys with lowercased drive letter; mirror that."""
    s = str(p.resolve())
    if len(s) >= 2 and s[1] == ":":
        s = s[0].lower() + s[1:]
    return s


def _section_header(project_dir: str) -> str:
    return f"[projects.{_toml_quote_key(project_dir)}]"


def _unquote_toml_key(raw: str) -> str | None:
    """Decode a TOML table-key fragment to its actual string value.

    Handles single-quoted literal strings (no escapes), double-quoted basic
    strings (with backslash escapes), and bare keys. Returns None if the
    input can't be sensibly decoded for our narrow purpose.
    """
    s = raw.strip()
    if not s:
        return None
    if s[0] == "'" and s.endswith("'") and len(s) >= 2:
        return s[1:-1]
    if s[0] == '"' and s.endswith('"') and len(s) >= 2:
        return _toml_basic_unescape(s[1:-1])
    # Bare key — letters, digits, underscores, dashes only per TOML, but be
    # permissive (Codex shouldn't produce bare keys for paths anyway).
    return s


def _toml_basic_unescape(s: str) -> str:
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            simple = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t",
                      "b": "\b", "f": "\f", "/": "/"}
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            if nxt == "u" and i + 6 <= n:
                try:
                    out.append(chr(int(s[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            if nxt == "U" and i + 10 <= n:
                try:
                    out.append(chr(int(s[i + 2:i + 10], 16)))
                    i += 10
                    continue
                except ValueError:
                    pass
            # Unknown escape — leave the backslash literal
            out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _parse_table_header(line: str) -> tuple[str, str] | None:
    """Parse a TOML table header line. Returns (dotted_key_text, trailing_comment)
    on success, else None.

    Walks the input character by character, respecting TOML's single-quoted
    literal strings and double-quoted basic strings (with backslash escapes),
    to find the closing `]`. Allows whitespace and an optional `# comment`
    after the closing bracket.
    """
    s = line.strip()
    if not s.startswith("["):
        return None
    n = len(s)
    i = 1
    while i < n:
        c = s[i]
        if c == "'":
            # Literal string — no escapes
            end = s.find("'", i + 1)
            if end == -1:
                return None
            i = end + 1
            continue
        if c == '"':
            # Basic string — handle backslash escapes
            i += 1
            terminated = False
            while i < n:
                if s[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if s[i] == '"':
                    i += 1
                    terminated = True
                    break
                i += 1
            if not terminated:
                return None
            continue
        if c == "]":
            key_text = s[1:i]
            tail = s[i + 1:].strip()
            if tail and not tail.startswith("#"):
                return None
            return key_text, tail
        i += 1
    return None


def _find_all_sections(lines: list[str], project_dir: str) -> list[tuple[int, int]]:
    """All (start, end) ranges for matching [projects.'...'] blocks, in order.

    Normally at most one, but a config corrupted by the pre-0.75.3 BOM bug can hold
    DUPLICATES: a leading U+FEFF hid the existing header from the section scan
    (`str.strip()` does not drop U+FEFF), so a second identical table was appended —
    invalid TOML the external codex CLI refuses to parse. Callers detect/repair that
    instead of silently editing only the first copy. Match is case-insensitive on the
    path; `end` is the index AFTER the block (next section header or EOF). Handles
    single-quoted literal and double-quoted basic-string keys + inline `# comment`.
    """
    target = project_dir.casefold()
    n = len(lines)
    out: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("[projects."):
            continue
        parsed = _parse_table_header(line)
        if parsed is None:
            continue
        key_text, _comment = parsed
        # key_text is `projects.<quoted-or-bare>`; strip the projects. prefix
        if not key_text.startswith("projects."):
            continue
        path = _unquote_toml_key(key_text[len("projects."):])
        if path is None or path.casefold() != target:
            continue
        # Find end of block (next section header or EOF)
        j = i + 1
        while j < n and not lines[j].strip().startswith("["):
            j += 1
        out.append((i, j))
    return out


def _find_section(lines: list[str], project_dir: str) -> tuple[int, int] | None:
    """Return (start, end) for the FIRST matching [projects.'...'] block, or None.
    See :func:`_find_all_sections` for the duplicate-table contract."""
    sections = _find_all_sections(lines, project_dir)
    return sections[0] if sections else None


def _collapse_duplicate_sections(lines: list[str], project_key: str) -> int:
    """Repair a BOM-corrupted config in place: keep the FIRST matching
    [projects.'<key>'] table and delete the rest (they are duplicates of the same
    managed project block, and duplicate tables are invalid TOML). Deletes from
    LAST to FIRST so earlier indices stay valid. Returns the number removed."""
    sections = _find_all_sections(lines, project_key)
    for start, end in sections[1:][::-1]:
        del lines[start:end]
    return max(0, len(sections) - 1)


def _parse_kv(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("["):
        return None
    if "=" not in stripped:
        return None
    k, v = stripped.split("=", 1)
    return k.strip(), v.strip()


def enable_project(config_path: Path, project_dir: Path) -> Result:
    """Ensure the project block has all three managed keys at target values."""
    project_key = _normalize_path(project_dir)
    config_path = config_path.expanduser()

    if not config_path.exists():
        body = _render_block(project_key)
        _atomic_write_text(config_path, body)
        return Result(
            action="created",
            config_path=config_path,
            project_dir=project_key,
            changes=[f"created config with {_section_header(project_key)}"] + list(MANAGED_KEYS),
        )

    # newline="" disables universal-newline translation on read, so a CRLF
    # config is detectable (read_text() default would silently fold \r\n to
    # \n, making the detector below dead code and rewriting the user's whole
    # file to LF — review M4a/L). We write back with newline="" too.
    text = _read_text_no_translate(config_path)
    # Preserve original newline style
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    # Strip trailing empty line artifact from split if file ended with newline
    trailing_newline = lines and lines[-1] == ""
    if trailing_newline:
        lines = lines[:-1]

    changes: list[str] = []
    # Self-repair a config corrupted by the pre-0.75.3 BOM bug (duplicate
    # [projects."<key>"] tables -> invalid TOML the codex CLI refuses). Collapse to
    # the first BEFORE applying managed keys, so every seed heals an affected user.
    removed_dups = _collapse_duplicate_sections(lines, project_key)
    if removed_dups:
        changes.append(f"removed {removed_dups} duplicate [projects] table(s)")

    section = _find_section(lines, project_key)

    if section is None:
        # Append a fresh block
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append(_section_header(project_key))
        for k in MANAGED_KEYS:
            lines.append(f"{k} = {TARGET_VALUES[k]}")
            changes.append(f"added {k}")
        action = "updated"
    else:
        start, end = section
        existing: dict[str, int] = {}  # key -> absolute line index
        for idx in range(start + 1, end):
            kv = _parse_kv(lines[idx])
            if kv and kv[0] in MANAGED_KEYS:
                existing[kv[0]] = idx

        # Update or insert each managed key
        # Insert position: just after the section header, after any keys already present
        insert_at = start + 1
        # Walk down past existing managed keys to find a sensible insert anchor
        while insert_at < end:
            kv = _parse_kv(lines[insert_at])
            if kv is None:
                break
            if kv[0] not in MANAGED_KEYS:
                break
            insert_at += 1

        # We'll build a fresh insertions list to apply at insert_at
        insertions: list[str] = []
        for k in MANAGED_KEYS:
            target = TARGET_VALUES[k]
            if k in existing:
                idx = existing[k]
                current_kv = _parse_kv(lines[idx])
                if current_kv and current_kv[1] != target:
                    lines[idx] = f"{k} = {target}"
                    changes.append(f"updated {k}")
            else:
                insertions.append(f"{k} = {target}")
                changes.append(f"added {k}")

        if insertions:
            lines[insert_at:insert_at] = insertions

        action = "updated" if changes else "unchanged"

    if trailing_newline:
        lines.append("")
    # newline="" → write the joined text verbatim. The line endings are
    # already baked in by split/join on the detected `newline`; letting
    # open() translate again would double them (\r\n -> \r\r\n) — review M4a.
    _atomic_write_text(config_path, newline.join(lines), newline="")
    return Result(action=action, config_path=config_path, project_dir=project_key, changes=changes)


def disable_project(config_path: Path, project_dir: Path) -> Result:
    """Remove approval_policy and sandbox_mode from the project block.

    Leaves trust_level alone (the user may rely on it for other Codex
    behaviour). Leaves the section in place even if it becomes empty so we
    don't accidentally destroy other keys the user added by hand.
    """
    project_key = _normalize_path(project_dir)
    config_path = config_path.expanduser()

    if not config_path.exists():
        return Result(action="no-op", config_path=config_path, project_dir=project_key,
                      changes=["config does not exist"])

    text = _read_text_no_translate(config_path)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    trailing_newline = lines and lines[-1] == ""
    if trailing_newline:
        lines = lines[:-1]

    dup_changes: list[str] = []
    removed_dups = _collapse_duplicate_sections(lines, project_key)
    if removed_dups:
        dup_changes.append(f"removed {removed_dups} duplicate [projects] table(s)")

    section = _find_section(lines, project_key)
    if section is None:
        return Result(action="no-op", config_path=config_path, project_dir=project_key,
                      changes=["project block not present"])

    start, end = section
    keys_to_remove = ("approval_policy", "sandbox_mode")
    removed: list[str] = []
    new_block: list[str] = []
    for idx in range(start, end):
        kv = _parse_kv(lines[idx])
        if kv and kv[0] in keys_to_remove:
            removed.append(f"removed {kv[0]}")
            continue
        new_block.append(lines[idx])

    if not removed and not dup_changes:
        return Result(action="unchanged", config_path=config_path, project_dir=project_key,
                      changes=["nothing to remove"])

    lines[start:end] = new_block
    if trailing_newline:
        lines.append("")
    # newline="" → write the joined text verbatim. The line endings are
    # already baked in by split/join on the detected `newline`; letting
    # open() translate again would double them (\r\n -> \r\r\n) — review M4a.
    _atomic_write_text(config_path, newline.join(lines), newline="")
    return Result(action="removed", config_path=config_path, project_dir=project_key,
                  changes=dup_changes + removed)


def repair_duplicate_project_tables(config_path: Path, project_dir: Path) -> int:
    """Collapse duplicate ``[projects.'<project>']`` tables to the first, healing a config
    a pre-0.75.3 BOM bug left with duplicates (invalid TOML the codex CLI refuses). Matches
    SEMANTICALLY via :func:`_find_all_sections` (so single/double/bare/escaped/case-variant
    key spellings of the SAME normalized path collapse — e.g. an operator's ``"x"`` header +
    agenttalk's canonical ``'x'`` header), and is SCOPED to ``project_dir`` (it never touches
    other tables). Idempotent; returns the number of duplicate tables removed. Used by the
    launch-time seed so an already-corrupted user heals on the next launch, and independent of
    the key-setting done by :func:`enable_project`."""
    config_path = config_path.expanduser()
    if not config_path.exists():
        return 0
    project_key = _normalize_path(project_dir)
    text = _read_text_no_translate(config_path)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    trailing_newline = lines and lines[-1] == ""
    if trailing_newline:
        lines = lines[:-1]
    removed = _collapse_duplicate_sections(lines, project_key)
    if removed:
        if trailing_newline:
            lines.append("")
        _atomic_write_text(config_path, newline.join(lines), newline="")
    return removed


def status(config_path: Path, project_dir: Path) -> dict:
    """Return current key states for the project block."""
    project_key = _normalize_path(project_dir)
    config_path = config_path.expanduser()
    result: dict = {
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "project_dir": project_key,
        "section_present": False,
        "duplicate_sections": 0,
        "keys": dict.fromkeys(MANAGED_KEYS),
    }
    if not config_path.exists():
        return result
    text = config_path.read_text(encoding="utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    sections = _find_all_sections(lines, project_key)
    if not sections:
        return result
    result["section_present"] = True
    # A pre-0.75.3 BOM-corrupted config can hold duplicate tables (invalid TOML);
    # report it so status does not falsely present a broken file as healthy.
    result["duplicate_sections"] = max(0, len(sections) - 1)
    start, end = sections[0]
    for idx in range(start + 1, end):
        kv = _parse_kv(lines[idx])
        if kv and kv[0] in MANAGED_KEYS:
            result["keys"][kv[0]] = kv[1]
    return result


def _render_block(project_dir: str) -> str:
    lines = [_section_header(project_dir)]
    for k in MANAGED_KEYS:
        lines.append(f"{k} = {TARGET_VALUES[k]}")
    lines.append("")
    return "\n".join(lines)
