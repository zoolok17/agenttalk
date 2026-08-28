"""The bundled Java adapter (approved PR-B plan, C-5: Java-only this
slice, sized to Amperian's backend).

DESIGN-55-comprehension-plane.md, Artifact 2, names the closed slice-1
relation vocabulary: ``import``, ``include``, ``inherit``, ``invoke``,
``route``, ``data``, ``configuration``, ``build``, ``test``. "An adapter
may emit a relation only when its versioned extraction rule names a
producer for that relation. Unsupported relation types remain coverage
gaps; they are never coerced into `data` or another healthy-looking
generic edge."

Per the lead's decided item-3 relation scope on the approved PR-B plan
(rq-cd8eac8f2bca dispatch, 2026-08-27):

    1. import, inherit, build, test - as planned.
    2. invoke - direct syntactic same-file/qualified static calls only, NO
       type resolution, evidence_class=extracted.
    3. route - ONLY as a named, annotation-DECLARED producer: Spring MVC
       request-mapping family annotations, and plain-XML web.xml
       servlet-mapping declarations when trivially present.
       evidence_class=declared.
    4. data, configuration - DEFERRED. Both would require call/type
       resolution to mean anything, which is inference, not declaration.
       Reported as EXPLICIT, ENUMERATED coverage gaps
       (``UNSUPPORTED_RELATIONS``), never silently omitted.

This module is a single-file, LOCAL adapter: it parses one file's bytes at
a time and emits CANDIDATE claims with unresolved/symbolic targets where
cross-file knowledge would be needed (design step 6, "Normalize records,
resolve only evidenced edges, merge declarations" - a separate, LATER,
global step over every adapter's claims, not this adapter's job). It is a
lightweight, pattern-based extractor deliberately, not a full Java
grammar/AST parser - this is coarse S1 evidence per the design's own
"smallest useful S1" framing; under-claim over guess.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import asdict, dataclass, field
from typing import Any

ADAPTER_NAME = "java"
ADAPTER_VERSION = 1
RULE_VERSION = 1

#: Relations this adapter does NOT attempt this slice - named, not hidden
#: (design: "Unsupported relation types remain coverage gaps").
UNSUPPORTED_RELATIONS = ("data", "configuration")

_TEST_PATH_SEGMENT = re.compile(r"(?:^|/)(?:src/test|test)/")
_TEST_NAME_SUFFIX = re.compile(r"(Test|Tests|IT)$")

_PACKAGE_RE = re.compile(r"(?m)^\s*package\s+([\w.]+)\s*;")
_IMPORT_RE = re.compile(r"(?m)^\s*import\s+(static\s+)?([\w.]+(?:\.\*)?)\s*;")
_TYPE_HEADER_RE = re.compile(
    r"\b(class|interface|enum)\s+(\w+)"
    r"(?:\s*<[^>{]*>)?"
    r"(?:\s+extends\s+([\w.<>,\s]+?))?"
    r"(?:\s+implements\s+([\w.<>,\s]+?))?"
    r"\s*\{",
    re.DOTALL,
)
_QUALIFIED_CALL_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\.([a-zA-Z_][A-Za-z0-9_]*)\s*\(")
_ROUTE_ANNOTATIONS = (
    "RequestMapping", "GetMapping", "PostMapping", "PutMapping",
    "DeleteMapping", "PatchMapping",
)
_ROUTE_ANNOTATION_RE = re.compile(
    r"@(" + "|".join(_ROUTE_ANNOTATIONS) + r")\b"
)
#: M-5 (third cold read, fix round 5): a verb-specific annotation names its
#: own HTTP method unambiguously; plain ``@RequestMapping`` does not (it
#: may carry a ``method = ...`` attribute this slice does not parse, or
#: default to every method) - ``None`` there rather than guessing. Two
#: different verbs on the SAME path are two distinct entry points to a
#: migration reader (a GET and a POST handler are different code), so the
#: method - when known - is folded into the route's own identity, not just
#: its path.
_ROUTE_METHOD_BY_ANNOTATION = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
}
#: M8 (cold-read, PR-B fix round 3): the route path/value attribute, by
#: NAME (Spring allows any attribute order - `produces = "...", value =
#: "/orders"` - blindly taking the first string literal in the argument
#: list previously captured the wrong one) or as a bare POSITIONAL string
#: (Spring's single-attribute shorthand, `@GetMapping("/orders")`, is
#: exactly `value`) - the latter only when it is the very first token, so
#: it can never be confused with a later, unrelated attribute's literal.
#:
#: B1 (fourth cold read, fix round 6): these ONLY locate the attribute
#: NAME/equals-sign (or the leading "(" for the positional case) - never
#: the quote or its content. That match runs against the SANITIZED
#: segment (comments/strings already blanked there), so a commented-out
#: `value = "..."` can never match this regex at all - its letters are
#: blanked to spaces along with the rest of the comment. The quote and
#: its content are recovered SEPARATELY, from the ORIGINAL text, starting
#: exactly at this match's end position (sanitization preserves length/
#: position exactly). See _route_path.
_ROUTE_NAMED_ATTR_RE = re.compile(r"\b(?:value|path)\s*=")
# The segment always starts with the annotation's own opening "(" (see
# _matching_close_paren's caller) - a bare positional literal is
# recognized only when it leads the argument list.
_ROUTE_POSITIONAL_ANCHOR_RE = re.compile(r"\A\(")
#: Recovers the quote that opens the target string literal, scanning the
#: ORIGINAL text from a position ALREADY PROVEN live (the end of a
#: _ROUTE_NAMED_ATTR_RE/_ROUTE_POSITIONAL_ANCHOR_RE match against the
#: sanitized segment) - only real whitespace/`{` may separate the two, so
#: this deliberately does NOT match through a comment wedged between the
#: attribute and its literal (a pathological case beyond this slice's
#: scope; the honest failure there is "no route found", never a
#: truncated or wrong one).
_ROUTE_VALUE_QUOTE_RE = re.compile(r'\s*\{?\s*"')
#: invariant 3 (design: "must not store... string-literal bodies"): a
#: route target is captured as a normalized route IDENTIFIER, never an
#: unbounded raw excerpt - truncated past this length rather than stored
#: verbatim regardless of source size.
_MAX_ROUTE_TARGET_LENGTH = 200


@dataclass(frozen=True)
class JavaUnitClaim:
    """One declared Java type (design, Artifact 1: a bundled adapter may
    additionally identify a package/module/component within a file
    unit)."""

    relative_path: str
    qualified_name: str
    simple_name: str
    line: int
    classification: str


@dataclass(frozen=True)
class JavaEdgeClaim:
    """One raw, LOCAL edge claim. ``target`` is a plain string (simple
    name, dotted name, or external identifier) - cross-file resolution
    into an actual internal ``unit_id`` is a later, global step (design
    step 6), not this adapter's job."""

    from_qualified_name: str
    relation: str
    target: str
    # "internal_candidate" | "internal_exact_or_external" |
    # "internal_static_import_exact_or_external" |
    # "internal_unqualified_call_candidate" | "external" |
    # "external_route" - see dependencies_artifact._edge_claim_to_record
    # for how each is resolved.
    target_kind: str
    evidence_class: str
    line: int | None
    phase: str
    #: M3 (fourth cold read, fix round 6): a Maven ``<dependency>``'s own
    #: ``<optional>true</optional>`` element - the ONE edge shape this
    #: adapter parses that has a declared optionality at all. False by
    #: default (every non-pom edge - import/inherit/invoke/route/test -
    #: has no such concept and stays False), never a guess for the pom
    #: case: unset or ``false`` in the pom is False, only an explicit
    #: ``true`` element flips it.
    optional: bool = False


@dataclass(frozen=True)
class JavaEntryPointClaim:
    qualified_name: str
    kind: str  # "cli_main" | "http_route"
    name: str
    line: int | None
    evidence_class: str


@dataclass(frozen=True)
class JavaFileResult:
    units: list[JavaUnitClaim] = field(default_factory=list)
    edges: list[JavaEdgeClaim] = field(default_factory=list)
    entry_points: list[JavaEntryPointClaim] = field(default_factory=list)


def _strip_comments_and_strings(text: str) -> str:
    """Blanks comment and string/char literal CONTENT with spaces while
    preserving every newline and the overall length/offsets, so a later
    regex match's position in the sanitized text is always the same
    position in the original (needed to recover a route annotation's real
    path string, which sanitization has otherwise blanked out)."""
    result: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            end = n if j == -1 else j
            result.append(" " * (end - i))
            i = end
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            end = n if j == -1 else j + 2
            segment = text[i:end]
            result.append("".join(c if c == "\n" else " " for c in segment))
            i = end
        elif ch == '"':
            if text[i:i + 3] == '"""':
                j = text.find('"""', i + 3)
                end = n if j == -1 else j + 3
            else:
                end = i + 1
                while end < n and text[end] != '"':
                    end += 2 if text[end] == "\\" and end + 1 < n else 1
                end = min(end + 1, n)
            segment = text[i:end]
            result.append("".join(c if c == "\n" else " " for c in segment))
            i = end
        elif ch == "'":
            end = i + 1
            while end < n and text[end] != "'":
                end += 2 if text[end] == "\\" and end + 1 < n else 1
            end = min(end + 1, n)
            segment = text[i:end]
            result.append("".join(c if c == "\n" else " " for c in segment))
            i = end
        else:
            result.append(ch)
            i += 1
    return "".join(result)


def _newline_offsets(text: str) -> list[int]:
    """Every newline's offset in ``text``, ascending - built ONCE per file
    so every per-claim line lookup (:func:`_line_at`) is O(log n), not
    O(file size) (M11, cold-read PR-B fix round 3: recomputing
    ``text.count("\\n", 0, offset)`` from offset 0 on every single call -
    once per import, per type, per invocation, per route match - made the
    adapter's total cost quadratic in file size; measured 0.27 MiB in
    0.79s, 0.53 MiB in 3.02s, 1.07 MiB in 12.33s, ~4x per doubling)."""
    return [i for i, ch in enumerate(text) if ch == "\n"]


def _line_at(newline_offsets: list[int], offset: int) -> int:
    # bisect_LEFT, not bisect_right: matches the original
    # `text.count("\n", 0, offset) + 1` semantics exactly - a newline AT
    # `offset` itself must not count as ending its own line early (a
    # position pointing AT a newline character is still on the line that
    # newline terminates).
    return bisect.bisect_left(newline_offsets, offset) + 1


def _classify(relative_path: str, simple_name: str | None) -> str:
    if _TEST_PATH_SEGMENT.search(relative_path.replace("\\", "/")):
        return "test"
    if simple_name and _TEST_NAME_SUFFIX.search(simple_name):
        return "test"
    return "production"


def _extract_types(
    sanitized: str, package: str | None,
) -> list[tuple[str, str, str, int, str | None, str | None, int]]:
    """Returns ``(qualified_name, simple_name, container_prefix, brace_pos,
    extends, implements_raw, end_brace_pos)`` for every declared type,
    correctly nested by tracking brace depth against each type header's own
    opening brace. ``end_brace_pos`` (the position of the type's OWN closing
    brace) is what :func:`_enclosing_qualified_name` uses to attribute a
    later match (a call, an annotation, a main method) to the innermost
    declared type whose body actually contains it, rather than to whichever
    type happened to be declared first in the file."""
    headers = list(_TYPE_HEADER_RE.finditer(sanitized))
    header_by_brace_pos = {m.end() - 1: m for m in headers}
    stack: list[tuple[int, str, int]] = []
    depth = 0
    results: list[list[Any]] = []
    for i, ch in enumerate(sanitized):
        if ch == "{":
            match = header_by_brace_pos.get(i)
            if match is not None:
                simple_name = match.group(2)
                # M-4 (third cold read, fix round 5): each stack entry's
                # own `name` is ALREADY that ancestor's full qualified
                # name (it was computed the same way, one level up) - the
                # immediate (innermost) enclosing entry alone IS the
                # correct prefix. Joining every entry's already-qualified
                # name together (the previous ".".join(...) over the
                # whole stack) instead concatenated each ancestor's own
                # full lineage AGAIN at every nesting level: a 3-deep type
                # (Outer/Inner/Innermost in package com.acme) qualified as
                # "com.acme.Outer.com.acme.Outer.Inner.Innermost" - wrong
                # from the second nesting level down, corrupting unit_id
                # (a hash of this string) and containment lookups for
                # every type nested 3+ deep. Invisible at depth 2, where
                # the stack holds only one entry and joining it with
                # nothing already happened to look identical to using it
                # directly.
                container_prefix = stack[-1][1] if stack else ""
                if container_prefix:
                    qualified = f"{container_prefix}.{simple_name}"
                elif package:
                    qualified = f"{package}.{simple_name}"
                else:
                    qualified = simple_name
                result_index = len(results)
                results.append([
                    qualified, simple_name, container_prefix,
                    match.start(), match.group(3), match.group(4), i,
                ])
                stack.append((depth, qualified, result_index))
            depth += 1
        elif ch == "}":
            depth -= 1
            if stack and stack[-1][0] == depth:
                _, _, result_index = stack.pop()
                results[result_index][6] = i
    return [tuple(result) for result in results]


def _enclosing_qualified_name(
    position: int,
    types: list[tuple[str, str, str, int, str | None, str | None, int]],
    fallback: str,
) -> str:
    """The innermost declared type whose ``[brace_pos, end_brace_pos]``
    span contains ``position`` - never just "the first type in the file"
    (Note 10, second cold read, fix round 4: a file with more than one
    top-level type attributed EVERY edge and entry point - regardless of
    which type's body the underlying call/annotation/main method actually
    appeared in - to the first declared type, misfiling real fan-in/fan-out
    and entry points onto the wrong unit whenever a second top-level type
    was present). Falls back to ``fallback`` (the file's primary type) for a
    position outside every known type body - e.g. an import statement,
    which precedes every type header and is legitimately file-scoped."""
    best: str | None = None
    best_span = None
    for qualified, _simple, _container, start, _extends, _implements, end in types:
        if start <= position <= end and (best_span is None or (end - start) < best_span):
            best = qualified
            best_span = end - start
    return best if best is not None else fallback


def _split_type_list(raw: str) -> list[str]:
    depth = 0
    parts: list[str] = []
    current = []
    for ch in raw:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def _matching_close_paren(sanitized: str, open_pos: int) -> int | None:
    """Returns the index of the ``)`` that BALANCES the ``(`` at
    ``open_pos`` in ``sanitized``, tracking nesting depth - or ``None`` if
    the parens never close before end of file. ``sanitized`` has already
    had every string/char literal's CONTENT blanked to spaces (by
    :func:`_strip_comments_and_strings`), so a paren character found here
    is always a real one, never one hiding inside a string literal.

    N10 (third cold read, fix round 5): the previous ``\\([^)]*\\)``
    regex captured up to the FIRST ``)`` found ANYWHERE in the argument
    list, with no awareness of nesting - an annotation argument
    containing its own nested call (``@RequestMapping(produces =
    someHelper(x, y), value = "/api/widgets")``) truncated the captured
    span right after that nested call's OWN closing paren, silently
    losing every attribute that followed it - including, in that
    example, the real ``value`` this whole mechanism exists to find."""
    depth = 0
    for i in range(open_pos, len(sanitized)):
        if sanitized[i] == "(":
            depth += 1
        elif sanitized[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _java_string_literal_content(text: str, quote_pos: int) -> str | None:
    """Given ``text[quote_pos] == '"'``, returns the string literal's
    inner content (excluding delimiters) - a triple-quoted text block's
    content between the ``\"\"\"`` markers, or an ordinary literal's
    content up to its own unescaped closing quote. ``None`` if the
    literal is never closed before end of file.

    B1 (fourth cold read, fix round 6): mirrors ``_strip_comments_and_
    strings``'s OWN boundary-finding logic exactly (same escaped-quote
    skip, same triple-quote handling) - the same rule that decides where
    a string ends for SANITIZATION purposes now also decides what its
    content IS for route-value extraction, closing the escaped-quote and
    text-block members of the nested-paren truncation family as a side
    effect of reusing one mechanism, not as separate fixes."""
    n = len(text)
    if text[quote_pos:quote_pos + 3] == '"""':
        content_start = quote_pos + 3
        close = text.find('"""', content_start)
        if close == -1:
            return None
        return text[content_start:close]
    end = quote_pos + 1
    while end < n and text[end] != '"':
        end += 2 if text[end] == "\\" and end + 1 < n else 1
    if end >= n:
        return None
    return text[quote_pos + 1:end]


def _route_path(sanitized: str, original: str, group_start: int, group_end: int) -> str | None:
    """Recover the annotation's literal path/value string. LOCATES the
    attribute (by name, or the leading positional literal) against the
    SANITIZED segment - comments and string content are already blanked
    there, so a commented-out ``value = "..."`` cannot match at all, its
    letters erased along with the rest of the comment. Then reads the
    literal's actual CONTENT from the ORIGINAL text, starting exactly at
    that match's end position (sanitization preserves length/position
    exactly).

    B1 (fourth cold read, fix round 6): the previous version matched the
    WHOLE named-attribute-plus-quoted-literal pattern against the
    ORIGINAL (unsanitized) text directly - comments are live text to that
    match, so a commented-out `value = "..."` preceding the real one won
    outright, publishing dead code as declared-class evidence AND as the
    entry point's own stable ID. Detection now happens where comments are
    already invisible; only content recovery ever touches the original.

    M8 (cold-read, PR-B fix round 3): looks up the named attribute first
    (position-independent - Spring allows any attribute order), falling
    back to a bare positional string only when it leads the argument
    list. The result is length-bounded (invariant 3), never an unbounded
    raw excerpt."""
    sanitized_segment = sanitized[group_start:group_end]
    match = _ROUTE_NAMED_ATTR_RE.search(sanitized_segment)
    if match is None:
        match = _ROUTE_POSITIONAL_ANCHOR_RE.match(sanitized_segment)
    if match is None:
        return None
    anchor = group_start + match.end()
    quote_match = _ROUTE_VALUE_QUOTE_RE.match(original, anchor)
    if quote_match is None:
        return None
    quote_pos = quote_match.end() - 1
    content = _java_string_literal_content(original, quote_pos)
    if content is None:
        return None
    return _bounded_route_target(content)


def _bounded_route_target(value: str) -> str:
    if len(value) <= _MAX_ROUTE_TARGET_LENGTH:
        return value
    return value[:_MAX_ROUTE_TARGET_LENGTH] + "...(truncated)"


#: M5 (fourth cold read, fix round 6): only whitespace and OTHER bare/
#: simple annotations may separate a class-level route annotation from
#: the type header it decorates (Java allows several annotations to
#: stack directly above one declaration, e.g. ``@RestController
#: @RequestMapping("/api/orders")``) - this is deliberately simpler than
#: the depth-tracking scan _matching_close_paren uses (no nested parens
#: inside one of these OTHER, skipped-over annotations): a real one
#: (`@SuppressWarnings({"x"})`) would fail this match and safely fall
#: back to treating the route annotation as method-level instead of
#: guessing at a class-level composition.
_ANNOTATION_TRIVIA_RE = re.compile(r"@\w+(?:\([^()]*\))?\s*|\s+")


def _class_level_route_target(
    sanitized: str, end_pos: int,
    types: list[tuple[str, str, str, int, str | None, str | None, int]],
) -> str | None:
    """If the annotation ending at ``end_pos`` sits DIRECTLY on a
    declared type - only trivia (whitespace, other stacked annotations)
    between it and that type's own header - returns the type's qualified
    name. ``None`` means this route annotation is on a method (or
    anything else), not a class."""
    header_starts = {start: qualified for qualified, _s, _c, start, _e, _i, _end in types}
    pos = end_pos
    n = len(sanitized)
    while pos < n:
        if pos in header_starts:
            return header_starts[pos]
        match = _ANNOTATION_TRIVIA_RE.match(sanitized, pos)
        if match is None or match.end() == pos:
            return None
        pos = match.end()
    return None


def _compose_route_path(prefix: str, path: str) -> str:
    """Spring's OWN declared composition semantics for a class-level
    ``@RequestMapping`` prefix plus a method-level route value - not
    inference (M5, fourth cold read, fix round 6): exactly one ``/``
    between the two, regardless of which side already has one."""
    return f"{prefix.rstrip('/')}/{path.lstrip('/')}"


def parse_java_source(relative_path: str, text: str) -> JavaFileResult:
    """Parse one ``.java`` file's TEXT (already read by the sanitized
    worker - this function never touches the filesystem itself)."""
    sanitized = _strip_comments_and_strings(text)
    newline_offsets = _newline_offsets(sanitized)
    package_match = _PACKAGE_RE.search(sanitized)
    package = package_match.group(1) if package_match else None

    imports = []
    import_simple_names: dict[str, str] = {}
    for match in _IMPORT_RE.finditer(sanitized):
        is_static = bool(match.group(1))
        target = match.group(2)
        imports.append((target, is_static, _line_at(newline_offsets, match.start())))
        if not target.endswith(".*"):
            import_simple_names[target.rsplit(".", 1)[-1]] = target

    types = _extract_types(sanitized, package)
    local_simple_names = {simple for _, simple, *_ in types}
    primary_qualified = types[0][0] if types else (package or relative_path)

    units = [
        JavaUnitClaim(
            relative_path=relative_path,
            qualified_name=qualified,
            simple_name=simple,
            line=_line_at(newline_offsets, brace_pos),
            classification=_classify(relative_path, simple),
        )
        for qualified, simple, _container, brace_pos, _extends, _implements, _end in types
    ]

    edges: list[JavaEdgeClaim] = []
    entry_points: list[JavaEntryPointClaim] = []

    for target, is_static, line in imports:
        # D-1 (reviewer-3, PR-B delta review round 2): a plain (non-static,
        # non-wildcard) import names a fully-qualified type that MAY be
        # declared inside this same scan - give it the same shot at
        # resolving internally that `extends`/`implements`/test-pairing
        # already get, via the exact same registry, never a guess. A
        # wildcard NON-static import names a package, not a type - the
        # part before ".*" can never be exact-matched against the unit
        # registry, so it stays plain external.
        #
        # N5 (fourth cold read, fix round 6): a STATIC import's target is
        # a member path (Type.MEMBER) or a static-member wildcard
        # (Type.*) - never itself a type's own qualified name - but in
        # BOTH cases the TYPE PREFIX (everything but the last segment) IS
        # itself fully qualified and exact-matchable, the exact same way
        # D-1 already established for a plain import. Stamping every
        # static import "external" unconditionally counted an internal
        # dependency (`import static com.acme.Foo.BAR` where `Foo` is
        # in-scan) as external, the same fan-in loss D-1 fixed for plain
        # imports. Member resolution itself stays out of scope - this
        # tracks the TYPE dependency, not which specific static member -
        # and the published target keeps the ORIGINAL full spelling
        # either way, for evidence.
        if is_static:
            target_kind = "internal_static_import_exact_or_external"
        elif target.endswith(".*"):
            target_kind = "external"
        else:
            target_kind = "internal_exact_or_external"
        edges.append(JavaEdgeClaim(
            from_qualified_name=primary_qualified, relation="import", target=target,
            target_kind=target_kind,
            evidence_class="extracted", line=line, phase="runtime",
        ))

    for qualified, simple, _container, brace_pos, extends, implements_raw, _end in types:
        line = _line_at(newline_offsets, brace_pos)
        if extends:
            for name in _split_type_list(extends):
                base = name.split("<", 1)[0].strip()
                if base:
                    edges.append(JavaEdgeClaim(
                        from_qualified_name=qualified, relation="inherit", target=base,
                        target_kind="internal_candidate", evidence_class="extracted",
                        line=line, phase="runtime",
                    ))
        if implements_raw:
            for name in _split_type_list(implements_raw):
                base = name.split("<", 1)[0].strip()
                if base:
                    edges.append(JavaEdgeClaim(
                        from_qualified_name=qualified, relation="inherit", target=base,
                        target_kind="internal_candidate", evidence_class="extracted",
                        line=line, phase="runtime",
                    ))
        if _TEST_NAME_SUFFIX.search(simple):
            under_test = _TEST_NAME_SUFFIX.sub("", simple)
            if under_test:
                edges.append(JavaEdgeClaim(
                    from_qualified_name=qualified, relation="test", target=under_test,
                    target_kind="internal_candidate", evidence_class="extracted",
                    line=line, phase="test",
                ))

    for match in _QUALIFIED_CALL_RE.finditer(sanitized):
        qualifier, _method = match.group(1), match.group(2)
        if qualifier in local_simple_names:
            # A type declared IN THIS SAME FILE - a known, non-ambiguous
            # local reference, safe to resolve with the full registry.
            target_kind = "internal_candidate"
        elif qualifier in import_simple_names:
            # Second cold read, B-1 (fix round 4): the qualifier resolves
            # through an import to a fully-qualified name - exactly the
            # shape D-1 already gives the SAME exact-match-or-external
            # treatment for the import edge itself. An import is how Java
            # spells "this call crosses a package boundary" - it does NOT
            # mean the target is external; it means the target is FULLY
            # QUALIFIED, which makes an EXACT registry lookup possible and
            # correct. Stamping this "external" unconditionally emptied
            # every cross-package internal call into the external bucket
            # (the NORMAL case in a real multi-package codebase), losing
            # the edge from fan-in and letting readiness claim
            # dependencies_resolved=satisfied over nothing.
            target_kind = "internal_exact_or_external"
            qualifier = import_simple_names[qualifier]
        else:
            # M12 (cold-read, PR-B fix round 3): neither locally declared
            # nor import-recognized - could be a genuine same-package
            # sibling (Java needs no import for that), but could equally
            # be a JDK/library type this extractor has no import evidence
            # for. Deliberately NOT "internal_candidate" here: that would
            # feed the GLOBAL simple-name matcher, and one same-named
            # class anywhere else in the whole scan (a JDK-shadowing name
            # like `Optional`, or a common test-helper name like `Assert`)
            # would then silently capture every unrelated call to that
            # name, codebase-wide - exactly the "invents an internal
            # target because names look similar" the design forbids. This
            # narrower kind only resolves via an EXACT qualified-name
            # match; otherwise it stays unresolved, never a guess.
            target_kind = "internal_unqualified_call_candidate"
        edges.append(JavaEdgeClaim(
            from_qualified_name=_enclosing_qualified_name(match.start(), types, primary_qualified),
            relation="invoke", target=qualifier,
            target_kind=target_kind, evidence_class="extracted",
            line=_line_at(newline_offsets, match.start()), phase="runtime",
        ))

    def _route_annotation_span(match: re.Match) -> tuple[int, str | None]:
        # N10 (third cold read, fix round 5): find the annotation's own
        # argument-list parens by tracking nesting depth (below), rather
        # than a regex that stopped at the FIRST close-paren anywhere in
        # the argument list - see _matching_close_paren's docstring for
        # the truncation this replaces. Returns (position right after
        # this annotation's own span, path-or-None) - the position is
        # what a class-level check must resume from, never match.end()
        # (which sits BEFORE this annotation's own arguments, not after
        # them).
        arg_pos = match.end()
        while arg_pos < len(sanitized) and sanitized[arg_pos].isspace():
            arg_pos += 1
        if arg_pos < len(sanitized) and sanitized[arg_pos] == "(":
            close_pos = _matching_close_paren(sanitized, arg_pos)
            if close_pos is not None:
                return close_pos + 1, _route_path(sanitized, text, arg_pos, close_pos + 1)
        return match.end(), None

    # M5 (fourth cold read, fix round 6): a class-level @RequestMapping is
    # a PREFIX for every method-level route inside that class - Spring's
    # own declared composition semantics, not inference (composing them
    # was previously never attempted at all: a class-level "/api/orders"
    # plus a method-level "/list" published as two independent routes,
    # the method's own published value "/list" a bare FRAGMENT of the
    # actually-served "/api/orders/list" in the field named for the whole
    # route). First pass: find every class-level route annotation (one
    # sitting directly on a type, not a method - see
    # _class_level_route_target) and record its literal prefix, only when
    # that literal was itself confidently extracted.
    class_route_prefix: dict[str, str] = {}
    for match in _ROUTE_ANNOTATION_RE.finditer(sanitized):
        span_end, path = _route_annotation_span(match)
        target_type = _class_level_route_target(sanitized, span_end, types)
        if target_type is not None and path is not None:
            class_route_prefix[target_type] = path

    for match in _ROUTE_ANNOTATION_RE.finditer(sanitized):
        line = _line_at(newline_offsets, match.start())
        enclosing = _enclosing_qualified_name(match.start(), types, primary_qualified)
        span_end, path = _route_annotation_span(match)
        if _class_level_route_target(sanitized, span_end, types) is not None:
            # A bare class-level annotation with no method-level mapping
            # inside that class represents no invocable route on its own
            # - already captured as a prefix above, never its own edge/
            # entry point.
            continue
        if path is not None and enclosing in class_route_prefix:
            path = _compose_route_path(class_route_prefix[enclosing], path)
        method = _ROUTE_METHOD_BY_ANNOTATION.get(match.group(1))
        if path is not None:
            target = f"{method} {path}" if method else path
        else:
            target = f"{enclosing}#{match.group(1)}"
        edges.append(JavaEdgeClaim(
            from_qualified_name=enclosing, relation="route", target=target,
            target_kind="external_route", evidence_class="declared",
            line=line, phase="runtime",
        ))
        entry_points.append(JavaEntryPointClaim(
            qualified_name=enclosing, kind="http_route",
            name=target, line=line, evidence_class="declared",
        ))

    # Note 10 (second cold read, fix round 4): finditer, not search - a
    # file with more than one top-level type can declare more than one
    # `main` method (e.g. two separate CLI entry classes in one file), and
    # the old single re.search silently kept only the first.
    for main_match in re.finditer(
        r"\bpublic\s+static\s+void\s+main\s*\(\s*String(?:\s*\[\s*\]|\.\.\.)\s+\w+\s*\)",
        sanitized,
    ):
        entry_points.append(JavaEntryPointClaim(
            qualified_name=_enclosing_qualified_name(main_match.start(), types, primary_qualified),
            kind="cli_main", name="main",
            line=_line_at(newline_offsets, main_match.start()), evidence_class="extracted",
        ))

    return JavaFileResult(units=units, edges=edges, entry_points=entry_points)


_XML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _strip_xml_comments(text: str) -> str:
    """Blanks XML comment content with spaces while preserving every
    newline and the overall length/offsets - mirrors
    ``_strip_comments_and_strings``'s Java-comment handling exactly.

    M-1 (second cold read, fix round 4): ``parse_maven_pom`` and
    ``parse_web_xml`` regexed the RAW xml with no comment stripping at
    all (unlike the Java path, which sanitizes first and has the proving
    test) - a commented-out ``<dependency>``/``<servlet-mapping>`` block
    published exactly as if it were live, declared evidence. Commented-out
    dependencies are common in legacy poms.
    """
    def _blank(match: re.Match) -> str:
        return "".join(c if c == "\n" else " " for c in match.group(0))
    return _XML_COMMENT_RE.sub(_blank, text)


#: M3 (fourth cold read, fix round 6): captures the WHOLE dependency
#: block rather than anchoring on groupId immediately followed by
#: artifactId - <optional>/<scope> (and <version>, ignored) can appear in
#: any order alongside them, the same "named attribute, any order" shape
#: the Spring route annotations already handle (M8, round 3). Non-greedy
#: so it stops at THIS dependency's own closing tag, never spanning into
#: a sibling <dependency> block.
_DEPENDENCY_BLOCK_RE = re.compile(r"<dependency>(.*?)</dependency>", re.DOTALL)
_DEPENDENCY_GROUP_ID_RE = re.compile(r"<groupId>\s*([^<]+?)\s*</groupId>")
_DEPENDENCY_ARTIFACT_ID_RE = re.compile(r"<artifactId>\s*([^<]+?)\s*</artifactId>")
#: Maven's own boolean spelling - never assumed False for anything but a
#: genuinely absent or explicit ``false`` element; only an explicit
#: ``true`` element makes an edge optional.
_DEPENDENCY_OPTIONAL_RE = re.compile(r"<optional>\s*(true|false)\s*</optional>", re.IGNORECASE)
#: Maven's own scope vocabulary (compile/provided/runtime/test/system/
#: import) - this slice maps only the one the design names explicitly
#: ("scope test -> phase test"); every other spelling (including no
#: <scope> at all, Maven's own "compile" default) stays this adapter's
#: existing "build" phase.
_DEPENDENCY_SCOPE_RE = re.compile(r"<scope>\s*([\w.-]+)\s*</scope>")


def parse_maven_pom(relative_path: str, text: str) -> list[JavaEdgeClaim]:
    """Direct-dependency ``build`` edges from a ``pom.xml``'s
    ``<dependency>`` blocks. Plain regex over a small, well-known XML
    shape - no XML parser (and its entity-expansion surface) needed for
    a handful of flat child elements.

    M3 (fourth cold read, fix round 6): ``<optional>``/``<scope>`` were
    read PAST and discarded - every edge asserted ``optional: false``,
    ``phase: build`` as a positive, hardcoded fact regardless of what the
    pom actually declared. Both are now parsed from the evidence already
    in the file: an explicit ``<optional>true</optional>`` sets
    ``optional``; ``<scope>test</scope>`` sets ``phase: test`` rather
    than the default ``build``."""
    from_name = relative_path
    sanitized = _strip_xml_comments(text)
    newline_offsets = _newline_offsets(sanitized)
    edges = []
    for match in _DEPENDENCY_BLOCK_RE.finditer(sanitized):
        block = match.group(1)
        group_match = _DEPENDENCY_GROUP_ID_RE.search(block)
        artifact_match = _DEPENDENCY_ARTIFACT_ID_RE.search(block)
        if group_match is None or artifact_match is None:
            continue
        group_id, artifact_id = group_match.group(1).strip(), artifact_match.group(1).strip()
        optional_match = _DEPENDENCY_OPTIONAL_RE.search(block)
        optional = optional_match is not None and optional_match.group(1).lower() == "true"
        scope_match = _DEPENDENCY_SCOPE_RE.search(block)
        scope = scope_match.group(1).strip().lower() if scope_match else None
        phase = "test" if scope == "test" else "build"
        edges.append(JavaEdgeClaim(
            from_qualified_name=from_name, relation="build",
            target=f"{group_id}:{artifact_id}", target_kind="external",
            evidence_class="declared", line=_line_at(newline_offsets, match.start()), phase=phase,
            optional=optional,
        ))
    return edges


_SERVLET_MAPPING_RE = re.compile(
    r"<servlet-mapping>\s*"
    r"<servlet-name>([^<]+)</servlet-name>\s*"
    r"<url-pattern>([^<]+)</url-pattern>",
)


def parse_web_xml(relative_path: str, text: str) -> list[JavaEntryPointClaim]:
    """``route`` entry points declared as plain ``<servlet-mapping>``/
    ``<url-pattern>`` pairs in a ``web.xml`` - the same "trivially present,
    named, no inference" bar as the annotation-based routes above."""
    entry_points = []
    sanitized = _strip_xml_comments(text)
    newline_offsets = _newline_offsets(sanitized)
    for match in _SERVLET_MAPPING_RE.finditer(sanitized):
        servlet_name, url_pattern = match.group(1).strip(), match.group(2).strip()
        entry_points.append(JavaEntryPointClaim(
            qualified_name=f"{relative_path}#{servlet_name}", kind="http_route",
            name=url_pattern, line=_line_at(newline_offsets, match.start()), evidence_class="declared",
        ))
    return entry_points


def file_result_to_json(result: JavaFileResult) -> dict[str, Any]:
    """Serializes a :class:`JavaFileResult` for the sanitized worker's
    stdout JSON channel (design: adapters run IN-PROCESS inside the
    worker, so their claims must cross the worker/parent process boundary
    the same way the worker's own file claims do - JSON over stdout, never
    a pickle or other code-carrying channel)."""
    return {
        "units": [asdict(u) for u in result.units],
        "edges": [asdict(e) for e in result.edges],
        "entry_points": [asdict(p) for p in result.entry_points],
    }


def file_result_from_json(payload: dict[str, Any]) -> JavaFileResult:
    return JavaFileResult(
        units=[JavaUnitClaim(**u) for u in payload["units"]],
        edges=[JavaEdgeClaim(**e) for e in payload["edges"]],
        entry_points=[JavaEntryPointClaim(**p) for p in payload["entry_points"]],
    )
