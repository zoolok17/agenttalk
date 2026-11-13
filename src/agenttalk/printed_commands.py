"""One rule for every command agenttalk prints for a person or an agent to copy (#727).

A stored value (a request id someone else chose, a file name, a path) pasted into a printed command can turn
it into something else: a space splits one argument into two, a ``;`` or ``&`` starts a second command, and
the Windows ``agenttalk.cmd`` launcher and Windows PowerShell 5.1 each re-read quotes in their own way, so no
quoting scheme is right in every route. The rule is therefore the simple one:

* a ready-to-run command is printed only when EVERY value in it is a plain token: ASCII letters, digits, ``.``,
  ``_`` and ``-``, starting with a letter or digit, the whole string (a trailing newline or a Unicode digit does
  not count);
* otherwise nothing is quoted. The command is shown with a ``<name>`` placeholder and a one-line note that
  shows the value escaped (see ``shown``), so the value can be looked up. The odd value itself is never in the
  command, but the text is still not a command to paste: a shell reads ``<name>`` as a redirection, so a placeholder
  that is made to sit inside a line somebody runs as it is must be left out of that line instead (the wrapper's
  reply line does).

A stored value that is echoed in a message (not in a command) goes through ``meta_text`` (an id) or ``line`` (free
text and paths), so it can never start a line of its own.

Ordinary ids and names are plain, so their commands print exactly as before.

``new_id_refusal`` is the second layer: where a NEW request or broadcast id is created from supplied text it must
fit the same kind of set (a longer limit), so a bad id is refused at the door with a clear message. Readers of
ids already stored are never tightened.
"""

from __future__ import annotations

import re
import unicodedata

_PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_NEW_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
NEW_ID_MAX_LENGTH = 96
SHOWN_MAX_CHARACTERS = 60
STORED_SOURCE = "is in the stored record this message is built from"  # where the whole of a stored value is
SUPPLIED_SOURCE = "is the text you supplied for this command"  # where the whole of a refused new value is
PATH_SHOWN_MAX_CHARACTERS = 260  # a path is shown whole up to the Windows path limit
# What a displayed value may contain as it is. Everything else, quotes and the backslash included, becomes a
# \uXXXX code, so the text holds no shell metacharacter, cannot start a new line and cannot hide or reorder text
# (CR, LF, NEL, U+2028/2029 and the bidi controls are all codes).
_SHOWN_AS_IS = re.compile(r"[A-Za-z0-9 ._,:/@+=-]")


def is_plain(value: object) -> bool:
    """True only for a string that is wholly a plain token."""
    return isinstance(value, str) and _PLAIN.fullmatch(value) is not None


def shown(value: object, limit: int = SHOWN_MAX_CHARACTERS, source: str | None = None) -> str:
    """A one-line, ASCII-only, double-quoted display of ``value`` for a message to a person. Not shell quoting.

    A value cut at ``limit`` characters says so, with the full length and where the whole value is; never copy a cut
    display as an id. ``source`` finishes the sentence "the whole value ..." and must be true for the caller: the
    default is right only for a value that was read from a stored record, so a value somebody just supplied (and
    that was never stored) or a path that came from the environment gives its own."""
    if not isinstance(value, str):
        return f"<a {type(value).__name__}, not text>"
    text, parts = value[:limit], []
    for ch in text:
        if _SHOWN_AS_IS.fullmatch(ch):
            parts.append(ch)
        else:
            code = ord(ch)
            parts.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
    cut = (
        f" [cut here: {len(value)} characters in all; the whole value {source or STORED_SOURCE}]"
        if len(value) > limit
        else ""
    )
    return '"' + "".join(parts) + '"' + cut


def cmds(
    templates: list[str],
    *,
    lookup: str | None = None,
    shown_limit: int = SHOWN_MAX_CHARACTERS,
    source: str | None = None,
    **values: object,
) -> tuple[list[str], str | None]:
    """Several ``templates`` filled in with the same ``values``: (the commands, a note or None).

    All values plain: the commands as they are, no note. Otherwise each odd value is a ``<name>`` placeholder (the
    odd value is not in the command, which is marked "not a ready command") and ONE note shows the escaped
    value(s); ``lookup`` is an optional plain-words way to find the real value. ``shown_limit`` is how much of a
    value the note shows (paths use the longer limit) and ``source`` says where the whole of a cut value is (see
    ``shown``)."""
    bad = [k for k, v in values.items() if not is_plain(v)]
    if not bad:
        return [t.format(**values) for t in templates], None
    shown_values = {k: (f"<{k}>" if k in bad else v) for k, v in values.items()}
    notes = ", ".join(f"<{k}> is {shown(values[k], shown_limit, source)}" for k in bad)
    note = f"not a ready command: {notes}, which is not a plain token" + (f"; {lookup}" if lookup else "")
    return [t.format(**shown_values) for t in templates], note


def cmd(
    template: str,
    *,
    lookup: str | None = None,
    shown_limit: int = SHOWN_MAX_CHARACTERS,
    source: str | None = None,
    **values: object,
) -> str:
    """``template`` with ``{name}`` filled in, as a ready command, only if every value is plain; otherwise the
    placeholder form with the note after it in parentheses (see ``cmds``)."""
    (text,), note = cmds([template], lookup=lookup, shown_limit=shown_limit, source=source, **values)
    return text if note is None else f"{text}  ({note})"


def line(value: object) -> str:
    """Free text or a path from a stored record, for a message to a person: as it is when it is ordinary, but never
    able to start a line of its own. Every control character (CR, LF, tab, NEL and the rest), every line or
    paragraph separator, every invisible or bidi format character, and any other character that has no printable
    form is written as ``\\uXXXX`` (``\\UXXXXXXXX`` above U+FFFF). A literal backslash is left alone, so a Windows
    path stays readable; text that already holds a ``\\u`` code reads the same as the escape it looks like, which
    only costs clarity, never safety."""
    if not isinstance(value, str):
        return f"<a {type(value).__name__}, not text>"
    parts = []
    for ch in value:
        if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"):
            code = ord(ch)
            parts.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
        else:
            parts.append(ch)
    return "".join(parts)


def meta_text(value: object) -> str:
    """A metadata value for a header line that is generated text: as it is when plain (or absent), else escaped."""
    return str(value) if value is None or is_plain(value) else shown(value)


def new_id_refusal(command: str, value: object, what: str = "request id") -> str | None:
    """None if ``value`` may be used as a NEW id; else the refusal text (one line, the value shown escaped)."""
    if isinstance(value, str) and _NEW_ID.fullmatch(value):
        return None
    return (
        f"agenttalk {command}: refusing - the new {what} {shown(value, source=SUPPLIED_SOURCE)} is not allowed: "
        "it must start with a letter "
        f"or digit and then use only letters, digits, '.', '_' or '-', at most {NEW_ID_MAX_LENGTH} characters. "
        "Nothing was sent."
    )
