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
_STRING_LITERAL_RE = re.compile(r'"([^"]*)"')


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
    # "internal_candidate" | "internal_exact_or_external" | "external" |
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


def _line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


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
    even though the sanitized copy blanked the string literal's content."""
    segment = original[group_start:group_end]
    literal = _STRING_LITERAL_RE.search(segment)
    return literal.group(1) if literal else None


def parse_java_source(relative_path: str, text: str) -> JavaFileResult:
    """Parse one ``.java`` file's TEXT (already read by the sanitized
    worker - this function never touches the filesystem itself)."""
    sanitized = _strip_comments_and_strings(text)
    package_match = _PACKAGE_RE.search(sanitized)
    package = package_match.group(1) if package_match else None

    imports = []
    import_simple_names: dict[str, str] = {}
    for match in _IMPORT_RE.finditer(sanitized):
        is_static = bool(match.group(1))
        target = match.group(2)
        imports.append((target, is_static, _line_at(sanitized, match.start())))
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
            line=_line_at(sanitized, brace_pos),
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
        line = _line_at(sanitized, brace_pos)
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
            target_kind = "internal_candidate"
        elif qualifier in import_simple_names:
            target_kind = "external"
            qualifier = import_simple_names[qualifier]
        else:
            target_kind = "internal_candidate"
        edges.append(JavaEdgeClaim(
            from_qualified_name=primary_qualified, relation="invoke", target=qualifier,
            target_kind=target_kind, evidence_class="extracted",
            line=_line_at(sanitized, match.start()), phase="runtime",
        ))

    for match in _ROUTE_ANNOTATION_RE.finditer(sanitized):
        line = _line_at(sanitized, match.start())
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
            line=_line_at(sanitized, main_match.start()), evidence_class="extracted",
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
    edges = []
    for match in _DEPENDENCY_RE.finditer(text):
        group_id, artifact_id = match.group(1).strip(), match.group(2).strip()
        edges.append(JavaEdgeClaim(
            from_qualified_name=from_name, relation="build",
            target=f"{group_id}:{artifact_id}", target_kind="external",
            evidence_class="declared", line=_line_at(text, match.start()), phase="build",
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
    for match in _SERVLET_MAPPING_RE.finditer(text):
        servlet_name, url_pattern = match.group(1).strip(), match.group(2).strip()
        entry_points.append(JavaEntryPointClaim(
            qualified_name=f"{relative_path}#{servlet_name}", kind="http_route",
            name=url_pattern, line=_line_at(text, match.start()), evidence_class="declared",
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
