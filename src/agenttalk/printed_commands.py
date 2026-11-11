"""One rule for every command agenttalk prints for a person or an agent to copy (#727).

A stored value (a request id someone else chose, a file name, a path) pasted into a printed command can turn
it into something else: a space splits one argument into two, a ``;`` or ``&`` starts a second command, and
the Windows ``agenttalk.cmd`` launcher and Windows PowerShell 5.1 each re-read quotes in their own way, so no
quoting scheme is right in every route. The rule is therefore the simple one:

* a ready-to-run command is printed only when EVERY value in it is a plain token: ASCII letters, digits, ``.``,
  ``_`` and ``-``, starting with a letter or digit, the whole string (a trailing newline or a Unicode digit does
  not count);
* otherwise nothing is quoted. The command is shown with a ``<name>`` placeholder and a one-line note that
  shows the value escaped (see ``shown``), so it can be looked up but not pasted by accident.

Ordinary ids and names are plain, so their commands print exactly as before.

``new_id_refusal`` is the second layer: where a NEW request or broadcast id is created from supplied text it must
fit the same kind of set (a longer limit), so a bad id is refused at the door with a clear message. Readers of
ids already stored are never tightened.
"""

from __future__ import annotations

import re

_PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_NEW_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
NEW_ID_MAX_LENGTH = 96
SHOWN_MAX_CHARACTERS = 60
PATH_SHOWN_MAX_CHARACTERS = 260  # a path is shown whole up to the Windows path limit
# What a displayed value may contain as it is. Everything else, quotes and the backslash included, becomes a
# \uXXXX code, so the text holds no shell metacharacter, cannot start a new line and cannot hide or reorder text
# (CR, LF, NEL, U+2028/2029 and the bidi controls are all codes).
_SHOWN_AS_IS = re.compile(r"[A-Za-z0-9 ._,:/@+=-]")


def is_plain(value: object) -> bool:
    """True only for a string that is wholly a plain token."""
    return isinstance(value, str) and _PLAIN.fullmatch(value) is not None


def shown(value: object, limit: int = SHOWN_MAX_CHARACTERS) -> str:
    """A one-line, ASCII-only, double-quoted display of ``value`` for a message to a person. Not shell quoting.

    A value cut at ``limit`` characters says so, with the full length and where the whole value is; never copy a cut
    display as an id."""
    if not isinstance(value, str):
        return f"<a {type(value).__name__}, not text>"
    text, parts = value[:limit], []
    for ch in text:
        if _SHOWN_AS_IS.fullmatch(ch):
            parts.append(ch)
        else:
            code = ord(ch)
            parts.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
    cut = (f" [cut here: {len(value)} characters in all; the whole value is in the stored record this message is "
           "built from]") if len(value) > limit else ""
    return '"' + "".join(parts) + '"' + cut


def cmds(
    templates: list[str], *, lookup: str | None = None, shown_limit: int = SHOWN_MAX_CHARACTERS, **values: object
) -> tuple[list[str], str | None]:
    """Several ``templates`` filled in with the same ``values``: (the commands, a note or None).

    All values plain: the commands as they are, no note. Otherwise each odd value is a ``<name>`` placeholder (so
    no command can run) and ONE note shows the escaped value(s); ``lookup`` is an optional plain-words way to find
    the real value. ``shown_limit`` is how much of a value the note shows (paths use the longer limit)."""
    bad = [k for k, v in values.items() if not is_plain(v)]
    if not bad:
        return [t.format(**values) for t in templates], None
    shown_values = {k: (f"<{k}>" if k in bad else v) for k, v in values.items()}
    notes = ", ".join(f"<{k}> is {shown(values[k], shown_limit)}" for k in bad)
    note = f"not a ready command: {notes}, which is not a plain token" + (f"; {lookup}" if lookup else "")
    return [t.format(**shown_values) for t in templates], note


def cmd(template: str, *, lookup: str | None = None, shown_limit: int = SHOWN_MAX_CHARACTERS, **values: object) -> str:
    """``template`` with ``{name}`` filled in, as a ready command, only if every value is plain; otherwise the
    placeholder form with the note after it in parentheses (see ``cmds``)."""
    (text,), note = cmds([template], lookup=lookup, shown_limit=shown_limit, **values)
    return text if note is None else f"{text}  ({note})"


def meta_text(value: object) -> str:
    """A metadata value for a header line that is generated text: as it is when plain (or absent), else escaped."""
    return str(value) if value is None or is_plain(value) else shown(value)


def new_id_refusal(command: str, value: object, what: str = "request id") -> str | None:
    """None if ``value`` may be used as a NEW id; else the refusal text (one line, the value shown escaped)."""
    if isinstance(value, str) and _NEW_ID.fullmatch(value):
        return None
    return (
        f"agenttalk {command}: refusing - the new {what} {shown(value)} is not allowed: it must start with a letter "
        f"or digit and then use only letters, digits, '.', '_' or '-', at most {NEW_ID_MAX_LENGTH} characters. "
        "Nothing was sent."
    )
