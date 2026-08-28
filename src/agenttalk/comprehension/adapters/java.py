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
    r"@(" + "|".join(_ROUTE_ANNOTATIONS) + r")\s*(\([^)]*\))?"
)
#: M8 (cold-read, PR-B fix round 3): the route path/value attribute, by
#: NAME (Spring allows any attribute order - `produces = "...", value =
#: "/orders"` - blindly taking the first string literal in the argument
#: list previously captured the wrong one) or as a bare POSITIONAL string
#: (Spring's single-attribute shorthand, `@GetMapping("/orders")`, is
#: exactly `value`) - the latter only when it is the very first token, so
#: it can never be confused with a later, unrelated attribute's literal.
_ROUTE_NAMED_VALUE_RE = re.compile(r'\b(?:value|path)\s*=\s*\{?\s*"([^"]*)"')
# The captured span includes the annotation's own enclosing parentheses
# (see _ROUTE_ANNOTATION_RE's group 2), so a bare positional literal is
# preceded by "(", not just whitespace/brace.
_ROUTE_POSITIONAL_VALUE_RE = re.compile(r'\A\(\s*\{?\s*"([^"]*)"')
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
    # "internal_unqualified_call_candidate" | "external" |
    # "external_route" - see dependencies_artifact._edge_claim_to_record
    # for how each is resolved.
    target_kind: str
    evidence_class: str
    line: int | None
    phase: str


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
) -> list[tuple[str, str, str, int, str | None, str | None]]:
    """Returns ``(qualified_name, simple_name, container_prefix, brace_pos,
    extends, implements_raw)`` for every declared type, correctly nested by
    tracking brace depth against each type header's own opening brace."""
    headers = list(_TYPE_HEADER_RE.finditer(sanitized))
    header_by_brace_pos = {m.end() - 1: m for m in headers}
    stack: list[tuple[int, str]] = []
    depth = 0
    results: list[tuple[str, str, str, int, str | None, str | None]] = []
    for i, ch in enumerate(sanitized):
        if ch == "{":
            match = header_by_brace_pos.get(i)
            if match is not None:
                simple_name = match.group(2)
                container_prefix = ".".join(name for _, name in stack)
                if container_prefix:
                    qualified = f"{container_prefix}.{simple_name}"
                elif package:
                    qualified = f"{package}.{simple_name}"
                else:
                    qualified = simple_name
                results.append((
                    qualified, simple_name, container_prefix,
                    match.start(), match.group(3), match.group(4),
                ))
                stack.append((depth, qualified))
            depth += 1
        elif ch == "}":
            depth -= 1
            if stack and stack[-1][0] == depth:
                stack.pop()
    return results


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


def _route_path(original: str, group_start: int, group_end: int) -> str | None:
    """Recover the annotation's literal path/value string from the
    ORIGINAL (un-sanitized) text at the SAME offsets the sanitized match's
    parenthesized argument group spans - sanitization preserves
    length/position exactly, so this is always the true source position,
    even though the sanitized copy blanked the string literal's content.

    M8 (cold-read, PR-B fix round 3): previously took the FIRST string
    literal in the argument list unconditionally, which is wrong whenever
    a different attribute's literal (e.g. `produces = "application/json"`)
    precedes the real `value`/`path` in source order. Looks up the named
    attribute first (position-independent), falling back to a bare
    positional string only when it leads the argument list. The result is
    length-bounded (invariant 3), never an unbounded raw excerpt."""
    segment = original[group_start:group_end]
    match = _ROUTE_NAMED_VALUE_RE.search(segment) or _ROUTE_POSITIONAL_VALUE_RE.match(segment)
    if match is None:
        return None
    return _bounded_route_target(match.group(1))


def _bounded_route_target(value: str) -> str:
    if len(value) <= _MAX_ROUTE_TARGET_LENGTH:
        return value
    return value[:_MAX_ROUTE_TARGET_LENGTH] + "...(truncated)"


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
        for qualified, simple, _container, brace_pos, _extends, _implements in types
    ]

    edges: list[JavaEdgeClaim] = []
    entry_points: list[JavaEntryPointClaim] = []

    for target, is_static, line in imports:
        # D-1 (reviewer-3, PR-B delta review round 2): a plain (non-static,
        # non-wildcard) import names a fully-qualified type that MAY be
        # declared inside this same scan - give it the same shot at
        # resolving internally that `extends`/`implements`/test-pairing
        # already get, via the exact same registry, never a guess. A
        # static import's target is a member path (ClassName.MEMBER), not
        # a type's own qualified name, and a wildcard import names a
        # package, not a type - neither can be exact-matched against the
        # unit registry, so both stay plain external.
        target_kind = (
            "external" if is_static or target.endswith(".*") else "internal_exact_or_external"
        )
        edges.append(JavaEdgeClaim(
            from_qualified_name=primary_qualified, relation="import", target=target,
            target_kind=target_kind,
            evidence_class="extracted", line=line, phase="runtime",
        ))

    for qualified, simple, _container, brace_pos, extends, implements_raw in types:
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
            from_qualified_name=primary_qualified, relation="invoke", target=qualifier,
            target_kind=target_kind, evidence_class="extracted",
            line=_line_at(newline_offsets, match.start()), phase="runtime",
        ))

    for match in _ROUTE_ANNOTATION_RE.finditer(sanitized):
        line = _line_at(newline_offsets, match.start())
        path = _route_path(text, match.start(2), match.end(2)) if match.group(2) else None
        target = path or f"{primary_qualified}#{match.group(1)}"
        edges.append(JavaEdgeClaim(
            from_qualified_name=primary_qualified, relation="route", target=target,
            target_kind="external_route", evidence_class="declared",
            line=line, phase="runtime",
        ))
        entry_points.append(JavaEntryPointClaim(
            qualified_name=primary_qualified, kind="http_route",
            name=target, line=line, evidence_class="declared",
        ))

    main_match = re.search(
        r"\bpublic\s+static\s+void\s+main\s*\(\s*String(?:\s*\[\s*\]|\.\.\.)\s+\w+\s*\)",
        sanitized,
    )
    if main_match is not None:
        entry_points.append(JavaEntryPointClaim(
            qualified_name=primary_qualified, kind="cli_main", name="main",
            line=_line_at(newline_offsets, main_match.start()), evidence_class="extracted",
        ))

    return JavaFileResult(units=units, edges=edges, entry_points=entry_points)


_DEPENDENCY_RE = re.compile(
    r"<dependency>\s*"
    r"<groupId>([^<]+)</groupId>\s*"
    r"<artifactId>([^<]+)</artifactId>",
)


def parse_maven_pom(relative_path: str, text: str) -> list[JavaEdgeClaim]:
    """Direct-dependency ``build`` edges from a ``pom.xml``'s
    ``<dependency>`` blocks. Plain regex over a small, well-known XML
    shape - no XML parser (and its entity-expansion surface) needed for
    two flat child elements."""
    from_name = relative_path
    newline_offsets = _newline_offsets(text)
    edges = []
    for match in _DEPENDENCY_RE.finditer(text):
        group_id, artifact_id = match.group(1).strip(), match.group(2).strip()
        edges.append(JavaEdgeClaim(
            from_qualified_name=from_name, relation="build",
            target=f"{group_id}:{artifact_id}", target_kind="external",
            evidence_class="declared", line=_line_at(newline_offsets, match.start()), phase="build",
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
    newline_offsets = _newline_offsets(text)
    for match in _SERVLET_MAPPING_RE.finditer(text):
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
