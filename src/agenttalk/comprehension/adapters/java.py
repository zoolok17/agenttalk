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
#: FIX ROUND 14 (tenth cold read, CR10-7 MINOR, wrong-data): a bare
#: name-suffix match alone is NOT corroborating evidence on its own - an
#: ordinary production class ending in "IT" (``AUDIT``, ``PROFIT``,
#: ``DEPOSIT``, any all-caps noun a legacy codebase happens to name a
#: class after) matched this suffix and published as unit_type=test
#: with a FABRICATED test edge to a nonexistent stripped-suffix target
#: (``AUDIT`` -> "AUD"). A name-suffix hit now requires CORROBORATION -
#: a test-framework import in the SAME file - to actually classify as
#: test or emit a test edge; a test SOURCE ROOT (below) is sufficient
#: evidence entirely on its own, no corroboration needed.
_TEST_NAME_SUFFIX = re.compile(r"(Test|Tests|IT)$")
#: PROVISIONAL, like every other closed-set constant in this package -
#: the well-known JUnit/TestNG import roots, not an exhaustive list of
#: every test framework a real codebase might use.
_TEST_FRAMEWORK_IMPORT_PREFIXES = ("org.junit", "junit.framework", "org.testng")

_PACKAGE_RE = re.compile(r"(?m)^\s*package\s+([\w.]+)\s*;")
_IMPORT_RE = re.compile(r"(?m)^\s*import\s+(static\s+)?([\w.]+(?:\.\*)?)\s*;")
#: BLOCKER 1a (fifth cold read, fix round 8): the OLD single fixed-shape
#: regex (`\b(class|interface|enum)\s+(\w+)(?:\s*<[^>{]*>)?(?:\s+
#: extends\s+([\w.<>,\s]+?))?(?:\s+implements\s+([\w.<>,\s]+?))?\s*\{`)
#: could not match a type-parameter list containing a NESTED generic
#: bound (`class Box<T extends Comparable<T>>` - `[^>{]*` cannot cross
#: the inner `<T>`'s own closing `>`, so the whole regex fails to match
#: at that position), an INTERSECTION bound (`class A<T extends Number &
#: Comparable<T>>` - same nested-`<>` problem), a `sealed ... permits
#: ...` header (nothing in the old pattern accounted for a `permits`
#: clause between `implements` and the body brace), or a `record`
#: declaration (not in the keyword alternation at all, and records have
#: their OWN parenthesized component list before the body brace). An
#: unmatched header dropped the type SILENTLY: zero units, the class-
#: level route prefix published as its own served entry point, the
#: method fragment published as the WHOLE route (the exact pre-M5 wrong
#: shapes), and readiness reported source_understood satisfied - status
#: complete, problem_count 0, on a file this adapter never actually
#: understood.
#:
#: Replaced with a two-stage scan: this anchor only locates the
#: KEYWORD and the type's own NAME (never a generic bound, a record
#: component, or a clause list) - genuinely nested/bracketed content is
#: then walked DEPTH-AWARE by _find_type_header_brace, exactly the
#: technique _matching_close_paren already uses for an annotation's own
#: argument list, rather than asking one fixed-shape regex to describe
#: everything between a type's name and its body in one shot.
_TYPE_NAME_ANCHOR_RE = re.compile(r"\b(class|interface|enum|record)\s+(\w+)")
#: Applied only to the CLAUSE ZONE _extract_types isolates (the text
#: between a type's own generic-parameter list/record-component list and
#: its body brace) - by that point a type parameter's own bound (which
#: may itself contain the word "extends") has already been skipped past
#: depth-aware, so these can safely take the first top-level match
#: without confusing a generic bound for the class's own superclass.
_HEADER_EXTENDS_RE = re.compile(r"\bextends\s+(.+?)(?=\s*\b(?:implements|permits)\b|\Z)", re.DOTALL)
_HEADER_IMPLEMENTS_RE = re.compile(r"\bimplements\s+(.+?)(?=\s*\bpermits\b|\Z)", re.DOTALL)
#: FIX ROUND 13 (ninth cold read, CR9-1 BLOCKER): the old pattern captured
#: only the LAST dotted segment before the method call - so a call
#: deliberately written fully qualified to disambiguate two same-simple-
#: name classes (``com.acme.legacy.OrderService.lookup(...)`` when both
#: ``com.acme.legacy.OrderService`` and an imported ``com.acme.v2.
#: OrderService`` exist) lost its own package prefix entirely, leaving a
#: bare "OrderService" that the invoke loop below then happily rewrote
#: via whichever import bound that simple name - publishing a resolved
#: dependency on the WRONG class and silently omitting the real one. The
#: optional leading group here captures every dotted segment (ANY case)
#: immediately preceding the final capitalized type segment, so the
#: qualifier carries the FULL dotted spelling when the source wrote one.
#:
#: FIX ROUND 13b (reviewer-3's B2 BLOCKER on round 13): the FIRST version
#: of this fix required the prefix segments to be lowercase-led
#: (package-shaped) specifically - so a NESTED type reference with a
#: package prefix (``com.acme.Outer.Inner.x()``) still reduced to its
#: bare tail ("Inner"), since "Outer" (capitalized) broke the all-
#: lowercase prefix match, and that bare tail then met the SAME bare-
#: keyed import table CR9-1 already closed one door on - resolving to an
#: unrelated imported ``Inner``, CR9-1's exact mechanism through the
#: second door. The prefix now accepts a dotted segment of ANY case, so
#: a dotted qualifier is NEVER reduced to less than the full chain the
#: source actually wrote - safe by construction even for a chain that
#: turns out not to be a real package+type reference at all (an object-
#: navigation-shaped false capture only ever yields an exact-match-or-
#: unresolved outcome downstream, never a wrong guess).
#:
#: A dotted qualifier never matches ``local_simple_names``/
#: ``import_simple_names`` below (both keyed by bare simple names), so
#: it always falls through to the exact-match-or-unresolved path -
#: inline-FQN evidence, never an import rewrite, the same discipline
#: round 12 already established for inherit/test.
_QUALIFIED_CALL_RE = re.compile(
    r"\b((?:[A-Za-z_$][\w$]*\.)*[A-Z][A-Za-z0-9_]*)\.([a-zA-Z_][A-Za-z0-9_]*)\s*\(")
#: FIX ROUND 13 (ninth cold read, CR9-2 MAJOR): the enumerated-recognizer
#: lesson (rounds 8/10 for headers/routes) applied here too - the old
#: pattern matched exactly ONE fixed token sequence ("public static void
#: main(String[] x)"/"...(String... x)"), so 6 of 9 legal spellings a real
#: javac accepts (modifier order - "static public"; C-style array after
#: the name - "String args[]"; a "final" parameter; "java.lang.String[]";
#: irregular whitespace; an extra modifier like "synchronized") went
#: silently undetected, and readiness published an AFFIRMATIVE "no entry
#: point" for a class that plainly has one - a confident negative from an
#: enumerated matcher, the exact class rounds 8/10 killed elsewhere.
#: De-enumerated by matching the SEMANTIC parts instead of one spelling:
#: a modifier-keyword run (checked programmatically for "public" AND
#: "static" present, in ANY order, alongside any other legal modifier),
#: then "void main(", then one parameter accepting every legal shape of a
#: String array (Java-style ``String[] x``, C-style ``String x[]``, or
#: varargs ``String... x``), with an optional ``final`` and an optional
#: ``java.lang.`` qualifier on the type, and flexible whitespace
#: throughout.
#:
#: FIX ROUND 13b (reviewer-3's B1 BLOCKER on round 13): round 13's own
#: totality claim was FALSE - the reviewer found FIVE more legal
#: spellings still missed (an annotation interleaved anywhere in the
#: modifier run, e.g. ``@Deprecated public static void main``; a type-
#: parameter section per JLS 8.4 between the modifiers and the return
#: type; a JSR-308 type annotation before the parameter type, e.g.
#: ``main(@NotNull String[] args)``; a JSR-308 annotation on the array
#: itself, e.g. ``main(String @NotNull [] args)``; combinations of the
#: above) - ``main(@NotNull String[] args)`` in particular is ordinary
#: real Java (JSR-305/JetBrains/Checker annotations applied uniformly
#: across a codebase), not an exotic edge case. Extended to the fuller
#: grammar the original comment already (wrongly) claimed to cover.
_MAIN_MODIFIER_KEYWORD = r"(?:public|static|final|synchronized|strictfp|native|abstract)"
#: An annotation, with or without a (possibly argument-carrying)
#: parenthesized clause - JSR-308 lets one appear before a modifier, a
#: type, or directly on an array level; never assumed absent anywhere
#: it might legally sit.
_MAIN_ANNOTATION = r"@[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*(?:\([^)]*\))?"
#: A single method-modifier-run TOKEN - either a modifier keyword or an
#: annotation, interleaved in any order (JLS 8.4.3: MethodModifier is
#: itself an alternation of keyword-or-annotation, repeatable).
_MAIN_MODIFIER_OR_ANNOTATION = (
    r"(?:\b" + _MAIN_MODIFIER_KEYWORD + r"\b|" + _MAIN_ANNOTATION + r")"
)
#: Any run of annotations and/or a "final", interleaved in any order,
#: immediately preceding the parameter's own type - covers both a type
#: annotation (``@NotNull String[] args``) and "final" in either
#: relative order with one.
_MAIN_PARAM_PREFIX = r"(?:(?:final\b|" + _MAIN_ANNOTATION + r")\s*)*"
#: An annotation run allowed directly on the array level itself (JSR-308
#: - ``String @NotNull [] args``), between the type and the brackets.
_MAIN_ARRAY_ANNOTATIONS = r"(?:" + _MAIN_ANNOTATION + r"\s*)*"
_MAIN_PARAM_RE = (
    _MAIN_PARAM_PREFIX + r"(?:java\.lang\.)?String"
    r"(?:\s*" + _MAIN_ARRAY_ANNOTATIONS + r"\[\s*\]\s*[A-Za-z_$][\w$]*"  # String[] args / String @X [] args
    r"|\s+[A-Za-z_$][\w$]*\s*" + _MAIN_ARRAY_ANNOTATIONS + r"\[\s*\]"    # String args[]  (C-style)
    r"|\s*\.\.\.\s*[A-Za-z_$][\w$]*"                                     # String... args
    r")"
)
#: Group 1 is the whole modifier/annotation run, captured (not just
#: matched) so the caller can check - programmatically, not by fixed
#: sequence - that BOTH "public" and "static" appear somewhere in it, in
#: any order, alongside whatever else (annotations, other modifiers) is
#: there. A regex alternation could match either keyword alone; only the
#: Python-side membership check below enforces both are required. An
#: optional JLS 8.4 TypeParameters section (``<T>``) may sit between the
#: modifier run and the return type - main is never actually generic in
#: valid usage, but the grammar allows the token there and this matcher
#: does not choke on it.
#: The modifier/annotation run is captured WITH its own trailing
#: whitespace (or the empty string, if there is none at all) so the
#: group can legally be ZERO occurrences - a completely bare, modifier-
#: less ``void main(...)`` is still a real, fully-parseable method
#: header (just certainly not a JVM entry point, missing both required
#: modifiers) and must be structurally RECOGNIZED, not fall through to
#: the class-closer's "unrecognized shape" bucket meant for something
#: this adapter genuinely cannot parse.
#:
#: FIX ROUND 13c (reviewer-3's rejection of round 13b): this used to be
#: ONE regex matching the modifier run THROUGH the closing paren, with a
#: separate, broader ``\bvoid\s+main\s*\(`` catch-all for anything it
#: missed. Split in two: this header-only pattern anchors the modifier
#: run and return type/name, stopping at the OPEN paren; the parameter
#: list itself is recovered separately via ``_matching_close_paren``
#: (below, in ``parse_java_source``) - depth-aware, so a JSR-308
#: annotation's own parenthesized argument inside the parameter list can
#: never be mistaken for the method's own closing paren.
_MAIN_HEADER_RE = re.compile(
    r"((?:" + _MAIN_MODIFIER_OR_ANNOTATION + r"(?:\s+" + _MAIN_MODIFIER_OR_ANNOTATION + r")*\s+)?)"
    r"(?:<[^>]*>\s*)?void\s+main\s*\("
)
#: The parameter list recovered between the header's open paren and its
#: matching close paren, anchored FULLY (start to end, only surrounding
#: whitespace allowed) - a partial match here would silently accept
#: trailing garbage after a recognized parameter.
_MAIN_PARAM_FULL_RE = re.compile(r"\A\s*" + _MAIN_PARAM_RE + r"\s*\Z")
#: FIX ROUND 13c (reviewer-3's MILDER ask): recovers a single parameter's
#: own leading TYPE token (after skipping any annotation/``final``
#: prefix) - used to tell a JLS-CERTAIN wrong-type negative (``main(int[]
#: args)`` - the JVM entry-point signature is exactly one ``String[]``
#: parameter, so any OTHER base type is unconditionally disqualifying,
#: regardless of spelling) apart from a genuinely unrecognized shape
#: (this adapter could not even determine a base type at all, or the
#: type IS ``String`` but in some array/varargs spelling not yet
#: recognized - the spelling-variant axis every enumerated-recognizer
#: lesson this producer has learned actually applies to).
_MAIN_PARAM_LEADING_TYPE_RE = re.compile(
    r"\A" + _MAIN_PARAM_PREFIX + r"((?:java\.lang\.)?[A-Za-z_$][\w$]*)")
_MAIN_STRING_TYPE_SPELLINGS = frozenset({"String", "java.lang.String"})
_ROUTE_ANNOTATIONS = (
    "RequestMapping", "GetMapping", "PostMapping", "PutMapping",
    "DeleteMapping", "PatchMapping",
)
#: Fix round 11 (seventh cold read BLOCKER, part 1 - de-enumerate
#: RECOGNITION): a FULLY-QUALIFIED route annotation
#: (``@org.springframework.web.bind.annotation.RequestMapping(...)``) was
#: previously invisible to this adapter entirely - the old pattern
#: anchored the simple name directly after ``@``, with no tolerance for
#: a preceding dotted qualifier. Recognizes the annotation by its dotted
#: name's LAST SEGMENT against the six families - the same rule
#: ``_TYPE_NAME_ANCHOR_RE``/the type extractor already applies to type
#: names - so a fully-qualified spelling is the same annotation, never a
#: silent miss.
_ROUTE_ANNOTATION_RE = re.compile(
    r"@(?:[A-Za-z_$][\w$]*\.)*(" + "|".join(_ROUTE_ANNOTATIONS) + r")\b"
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
#: N2 (fifth cold read, fix round 8): a plain @RequestMapping's own
#: ``method = RequestMethod.X`` attribute was never parsed at all - two
#: @RequestMapping routes on the SAME path, differing only by this
#: attribute, both published with no method prefix (unlike
#: @GetMapping/@PostMapping, which fold their own verb implicitly) and
#: silently COALESCED into one entry point by round 5's own coalescing
#: rule - correct for a genuine duplicate, wrong here since these are
#: two different handlers. Adjacent machinery (_ROUTE_METHOD_BY_
#: ANNOTATION) already folds the method into the route's identity for
#: the verb-specific annotations; this closes the one shape it did not
#: cover, rather than merely declaring the gap.
#:
#: N4/MAJOR 1 fold-in (sixth cold read, fix round 10): the attribute's
#: OWN value can itself be a braced, multi-value array
#: (``method = {RequestMethod.GET, RequestMethod.POST}``) - the old
#: regex captured only the first RequestMethod.X inside it (matching
#: right through the brace), silently re-coalescing two distinct
#: handlers into one. Captures the whole attribute value (braced or
#: bare, up to the next top-level ``,``/``)``) and every verb spelling
#: inside it is recovered below.
#:
#: N1 (seventh cold read, fix round 11 - de-enumerate the SAME way as
#: annotation recognition): the old value regex REQUIRED the literal
#: qualifier ``RequestMethod.`` immediately before the verb name - a
#: static-imported bare constant (``method = GET``, no qualifier present
#: in the source AT ALL) never matched, and a differently-qualified
#: fully-qualified spelling
#: (``org.springframework.web.bind.annotation.RequestMethod.GET``) had
#: no ``RequestMethod.`` substring positioned where the regex required
#: it either - both silently coalesced two distinct handlers sharing a
#: path into one (neither recognized its own explicit method, both fell
#: back to method-unknown, publishing the SAME target). The qualifier -
#: however spelled, or absent entirely - is now optional and unenumerated:
#: only the LAST segment (the enum constant's own name) is ever
#: significant, the same trust the original design already placed in
#: "RequestMethod.ANYTHING" being a real, compiler-validated enum
#: constant, extended to not caring how - or whether - it is qualified.
_ROUTE_METHOD_ATTR_RE = re.compile(r"\bmethod\s*=\s*(\{[^}]*\}|[^,)]+)")
_ROUTE_METHOD_VALUE_RE = re.compile(r"(?:[A-Za-z_$][\w$]*\.)*([A-Za-z_$][\w$]*)")
#: FIX ROUND 13 (ninth cold read, CR9-4 MINOR, wrong-data): Spring's own
#: ``RequestMethod`` enum is closed - these are every constant it
#: declares. A non-enum identifier (a random constant, e.g.
#: ``method = HttpConstants.READ_METHOD``, or a typo) used to publish
#: VERBATIM as if it were a real HTTP verb - the tool inventing a verb
#: Spring itself never recognizes. Every neighbouring unrecoverable case
#: in this adapter suppresses and records rather than guesses; the
#: chosen treatment here is the NARROWER of the two round-11-consistent
#: options - drop only the invalid verb (falling back to the bare,
#: method-unknown path, exactly like a plain @RequestMapping with no
#: method attribute at all - already a legitimate, unflagged state per
#: M-5/round 5) rather than suppressing the whole route with a problem.
#: The underlying PATH is still genuine, correctly-recovered evidence;
#: only the verb annotation was unreadable, and Spring itself resolves
#: this at request time regardless of what verb this tool can name.
_REQUEST_METHOD_VOCABULARY = frozenset({
    "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE",
})


def _route_method_attributes(sanitized_segment: str) -> list[str]:
    match = _ROUTE_METHOD_ATTR_RE.search(sanitized_segment)
    if match is None:
        return []
    recovered = [name.upper() for name in _ROUTE_METHOD_VALUE_RE.findall(match.group(1))]
    return [name for name in recovered if name in _REQUEST_METHOD_VOCABULARY]


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
#: position exactly). See _route_paths.
_ROUTE_NAMED_ATTR_RE = re.compile(r"\b(?:value|path)\s*=")
# The segment always starts with the annotation's own opening "(" (see
# _matching_close_paren's caller) - a bare positional literal is
# recognized only when it leads the argument list.
_ROUTE_POSITIONAL_ANCHOR_RE = re.compile(r"\A\(")
#: FIX ROUND 13 (ninth cold read, CR9-3 MAJOR, completeness): the first
#: token after the opening "(" may be a DIFFERENT named attribute
#: entirely (``produces = "..."``, ``consumes = "..."``, ...) rather than
#: an attempted positional value/path literal - Spring allows a route
#: annotation with ONLY these attributes and no value/path at all,
#: legitimately serving the enclosing prefix alone, same as a bare
#: ``@GetMapping``. Without this check, that shape read as "a positional
#: literal was attempted here but is unreadable" and suppressed the
#: whole route as unrecoverable - factually wrong, since no value
#: expression was ever written for this annotation to fail to recover.
_ROUTE_LEADING_NAMED_ATTR_RE = re.compile(r"\s*[A-Za-z_$][\w$]*\s*=(?!=)")
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
    # "internal_static_import_exact_or_external" | "external" |
    # "external_route" - see dependencies_artifact._edge_claim_to_record
    # for how each is resolved. FIX ROUND 14 (CR10-2): retired
    # "internal_unqualified_call_candidate" - invoke's bare/dotted
    # qualifier now shares "internal_candidate"'s own ladder with
    # inherit/test, never a narrower, separately-maintained kind.
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
class JavaAdapterProblem:
    """One thing this adapter could not confidently do while parsing a
    file - never silently published as a guess, never silently dropped
    either. ``reason_code`` distinguishes the FAMILY (fix round 11:
    association failure vs. value-recovery failure are different,
    separately-named problems, not one generic bucket) - the worker
    surfaces each as its own named ``WorkerProblem`` (worker.py).

    FIX ROUND 13c (reviewer-3's part 1 on round 13b): ``qualified_name``
    - ``None`` by default - names the ONE declared type this problem is
    actually ABOUT, when the adapter can pin one down (e.g. an
    unrecognized cli_main-like method belongs to its own enclosing
    type, never every sibling type in the same file). ``None`` keeps
    today's file-wide broadcast for problem kinds with no single owning
    type (a route fail-safe, a whole-file parse failure) - unchanged."""

    reason_code: str
    detail: str
    qualified_name: str | None = None


@dataclass(frozen=True)
class JavaFileResult:
    units: list[JavaUnitClaim] = field(default_factory=list)
    edges: list[JavaEdgeClaim] = field(default_factory=list)
    entry_points: list[JavaEntryPointClaim] = field(default_factory=list)
    problems: list[JavaAdapterProblem] = field(default_factory=list)


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


def is_effectively_empty_java_source(text: str) -> bool:
    """BLOCKER 1b (fifth cold read, fix round 8): True when NOTHING
    remains once comments/strings are blanked and any package/import
    statements are removed - genuinely no top-level declaration for an
    adapter to have understood (a legitimately typeless file: blank,
    comment-only, or package/import statements alone), never a real
    declaration whose header this adapter's coarse pattern-based
    extractor simply failed to recognize. The worker calls this to
    distinguish the two before deciding whether a zero-unit parse result
    is a named, explicit non-problem or a real ``no_types_extracted``
    problem - closing the zero-extraction evidence hole as a class
    without silently exempting every legitimately typeless file too."""
    sanitized = _strip_comments_and_strings(text)
    remainder = _PACKAGE_RE.sub("", sanitized)
    remainder = _IMPORT_RE.sub("", remainder)
    return remainder.strip() == ""


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


def _classify(
    relative_path: str, simple_name: str | None, *, has_test_framework_evidence: bool = False,
) -> str:
    if _TEST_PATH_SEGMENT.search(relative_path.replace("\\", "/")):
        return "test"
    if simple_name and _TEST_NAME_SUFFIX.search(simple_name) and has_test_framework_evidence:
        return "test"
    return "production"


def _matching_close_angle(sanitized: str, open_pos: int) -> int | None:
    """Mirrors :func:`_matching_close_paren` for a type's own generic
    parameter list - depth-aware over ``<``/``>`` so a BOUNDED
    (``<T extends Comparable<T>>``) or INTERSECTION (``<T extends
    Number & Comparable<T>>``) bound's own nested ``<...>`` does not
    truncate the scan at the bound's inner closing ``>`` (BLOCKER 1a,
    fifth cold read, fix round 8). Bails out (``None``) on a top-level
    ``;`` before ever closing - a real generic parameter list never
    contains a bare statement terminator; reaching one means this was
    never really one (a `<` used as a less-than operator, or malformed
    input), the same safe non-guess this adapter makes everywhere else."""
    depth = 0
    for i in range(open_pos, len(sanitized)):
        ch = sanitized[i]
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
            if depth == 0:
                return i
        elif ch == ";" and depth == 1:
            return None
    return None


def _skip_bracketed(sanitized: str, pos: int, open_ch: str, matcher) -> int:
    """If the next non-whitespace character at/after ``pos`` is
    ``open_ch``, returns the position right after its DEPTH-AWARE
    matching close (via ``matcher``, one of :func:`_matching_close_angle`
    or :func:`_matching_close_paren`) - otherwise returns ``pos``
    unchanged (there was nothing to skip)."""
    n = len(sanitized)
    p = pos
    while p < n and sanitized[p].isspace():
        p += 1
    if p < n and sanitized[p] == open_ch:
        close = matcher(sanitized, p)
        if close is not None:
            return close + 1
    return pos


def _find_type_header_brace(sanitized: str, clause_start: int) -> int | None:
    """BLOCKER 1a (fifth cold read, fix round 8): starting right AFTER a
    type's own generic-parameter list and (for a record) its component
    list have already been skipped, finds this type's own opening brace
    - depth-aware over both ``<...>`` (a generic bound inside an
    ``extends``/``implements``/``permits`` clause, e.g. ``implements
    Comparable<Foo<Bar>>``) and ``(...)`` (defensive: a record's
    component list already skipped by the caller, or any other
    parenthesized construct that might otherwise hide a stray brace)
    so neither construct's own characters are mistaken for the type's
    REAL body brace. ``None`` if a top-level ``{`` is never reached
    before a top-level ``;`` or end of file - not a real type header
    (a false-positive keyword match on sanitized text, or a shape this
    adapter does not resolve), never a guess at where the body begins."""
    n = len(sanitized)
    angle_depth = 0
    paren_depth = 0
    for i in range(clause_start, n):
        ch = sanitized[i]
        if ch == "<":
            angle_depth += 1
        elif ch == ">":
            if angle_depth > 0:
                angle_depth -= 1
        elif ch == "(":
            paren_depth += 1
        elif ch == ")":
            if paren_depth > 0:
                paren_depth -= 1
        elif angle_depth == 0 and paren_depth == 0:
            if ch == "{":
                return i
            if ch == ";":
                return None
    return None


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
    type happened to be declared first in the file.

    BLOCKER 1a (fifth cold read, fix round 8): each candidate header is
    now located in two stages - _TYPE_NAME_ANCHOR_RE anchors only the
    keyword and the type's own name (a fixed, simple shape that can
    never itself be ambiguous), then a depth-aware scan (_skip_bracketed
    + _find_type_header_brace) walks past a possibly-nested generic
    parameter list, a record's own component list, and any extends/
    implements/permits clause to the type's real body brace - closing
    the generic-bounded, sealed+permits, and record header shapes the
    old single fixed-shape regex could not match at all."""
    header_by_brace_pos: dict[int, tuple[int, str, str | None, str | None]] = {}
    for name_match in _TYPE_NAME_ANCHOR_RE.finditer(sanitized):
        # BLOCKER, second report (sixth cold read, fix round 9b): round
        # 9's own fix tightened the "record" anchor (a mandatory
        # component list) but left a DIFFERENT, also real variant open -
        # a CLASS LITERAL (`Foo.class`) is itself valid Java grammar in
        # any expression position (e.g. `String.class instanceof
        # Object`), and "class" there is followed by whitespace then an
        # ordinary identifier ("instanceof") the SAME shape a real
        # declaration has. The reviewer's own guard: reject a type-name
        # anchor immediately preceded (skipping whitespace) by a member-
        # access dot - class/interface/enum/record are NEVER legitimately
        # preceded by "." in a real declaration, so this never narrows
        # real support, only rejects a literal/member-access reading.
        dot_probe = name_match.start() - 1
        while dot_probe >= 0 and sanitized[dot_probe].isspace():
            dot_probe -= 1
        if dot_probe >= 0 and sanitized[dot_probe] == ".":
            continue
        clause_start = _skip_bracketed(sanitized, name_match.end(), "<", _matching_close_angle)
        if name_match.group(1) == "record":
            # MINOR 4 (sixth cold read, fix round 9): "record" is a
            # CONTEXTUAL keyword - unlike class/interface/enum (fully
            # reserved), it remains legal as an ordinary identifier (a
            # variable/parameter literally named "record"). A REAL
            # record declaration always has a component parameter list,
            # even an empty one (`record Foo() {}` is the minimal valid
            # form; `record Foo {}` is not valid Java at all) - requiring
            # it here rejects "record" used as a plain identifier
            # immediately followed by another word (most plausibly the
            # "instanceof" operator: "void m(Object record) { if (record
            # instanceof String s) ... }" previously matched, publishing
            # a phantom unit named after whatever word followed).
            probe = clause_start
            while probe < len(sanitized) and sanitized[probe].isspace():
                probe += 1
            if probe >= len(sanitized) or sanitized[probe] != "(":
                continue
            clause_start = _skip_bracketed(sanitized, clause_start, "(", _matching_close_paren)
        brace_pos = _find_type_header_brace(sanitized, clause_start)
        if brace_pos is None:
            continue
        clause_text = sanitized[clause_start:brace_pos]
        extends_match = _HEADER_EXTENDS_RE.search(clause_text)
        implements_match = _HEADER_IMPLEMENTS_RE.search(clause_text)
        header_start = name_match.start()
        if name_match.group(1) == "interface":
            # Round 10c (reviewer-3 delta on round 10b): an ANNOTATION-
            # TYPE declaration (`@interface Name { ... }`) is a
            # first-class extracted header whose span starts at its OWN
            # `@` - not at the bare `interface` keyword - so a route
            # annotation stacked on it (Spring's own composed-annotation
            # idiom: this is literally how @GetMapping et al. are
            # defined) associates into a REAL header span the same way
            # any other stacked annotation on any other declaration
            # does, via the existing backward-anchoring machinery.
            # Round 10b's _next_header_is_annotation_type_declaration
            # special case (a nearest-following-extracted-header
            # proximity test with no adjacency requirement - reviewer-3's
            # new minor: when the genuinely-offending declaration is
            # itself unmatchable, the test skips past it to an unrelated
            # later `@interface` and wrongly exempts) is deleted; there
            # is no special case left to keep in step.
            probe = header_start
            while probe > 0 and sanitized[probe - 1].isspace():
                probe -= 1
            if probe > 0 and sanitized[probe - 1] == "@":
                header_start = probe - 1
        header_by_brace_pos[brace_pos] = (
            header_start, name_match.group(2),
            extends_match.group(1).strip() if extends_match else None,
            implements_match.group(1).strip() if implements_match else None,
        )

    stack: list[tuple[int, str, int]] = []
    depth = 0
    results: list[list[Any]] = []
    for i, ch in enumerate(sanitized):
        if ch == "{":
            header = header_by_brace_pos.get(i)
            if header is not None:
                header_start, simple_name, extends_raw, implements_raw = header
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
                    header_start, extends_raw, implements_raw, i,
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


def _position_inside_any_type_body(
    position: int,
    types: list[tuple[str, str, str, int, str | None, str | None, int]],
) -> bool:
    """Whether ``position`` falls inside SOME declared type's own
    ``[header_start, end_brace_pos]`` span - fix round 10's fail-safe
    needs this DIRECTLY (a plain containment test), not via
    :func:`_enclosing_qualified_name`'s resolved NAME: in a single-type
    file, that name and the file's own ``fallback`` (primary_qualified)
    are the SAME string by coincidence, so comparing names could not
    tell "genuinely outside every type" apart from "inside the file's
    only type" - the exact case this fail-safe must never fire on."""
    return any(start <= position <= end for _q, _s, _c, start, _e, _i, end in types)


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


def _split_top_level_commas(text: str) -> list[str]:
    """FIX ROUND 13c (reviewer-3's MILDER ask): splits a recovered
    parameter-list substring on TOP-LEVEL commas only - depth-aware
    across ``()``, ``[]``, and ``<>`` so a nested annotation argument,
    array dimension, or generic type argument's own comma is never
    mistaken for a parameter separator. Returns ``[]`` for whitespace-
    only text (zero parameters - ``main()``), never ``[""]``, so the
    caller can count arity directly off ``len(...)``."""
    if not text.strip():
        return []
    parts = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch in "([<":
            depth += 1
        elif ch in ")]>":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _matching_open_paren(sanitized: str, close_pos: int) -> int | None:
    """Backward mirror of :func:`_matching_close_paren`: given
    ``sanitized[close_pos] == ')'``, returns the index of the ``(`` that
    BALANCES it, tracking nesting depth backward - or ``None`` if
    unbalanced. Fix round 10 (structural order): the one new primitive
    backward-anchoring needs, so a stacked annotation's own argument list
    can be walked BACKWARD from a type header exactly as confidently as
    it is already walked forward from the annotation's own name."""
    depth = 0
    for i in range(close_pos, -1, -1):
        if sanitized[i] == ")":
            depth += 1
        elif sanitized[i] == "(":
            depth -= 1
            if depth == 0:
                return i
    return None


#: Minor 6 (fifth cold read, fix round 7): Java's own single-character
#: escape sequences (JLS 3.10.7) - each maps to the ONE character it
#: actually represents, never the two-character raw spelling.
_JAVA_SIMPLE_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
    "s": " ", "0": "\0", '"': '"', "'": "'", "\\": "\\",
}
_JAVA_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _decode_java_string_escapes(raw: str) -> str:
    """Unescapes a Java string/text-block literal's raw source spelling
    into the character sequence it actually represents (JLS 3.10.6/
    3.10.7) - a `\\"` in source is ONE quote character at runtime, not
    two literal characters; an unrecognized escape is left as-is rather
    than guessed at."""
    result: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n:
            unicode_match = _JAVA_UNICODE_ESCAPE_RE.match(raw, i)
            if unicode_match is not None:
                result.append(chr(int(unicode_match.group(1), 16)))
                i = unicode_match.end()
                continue
            simple = _JAVA_SIMPLE_ESCAPES.get(raw[i + 1])
            if simple is not None:
                result.append(simple)
                i += 2
                continue
        result.append(ch)
        i += 1
    return "".join(result)


def _normalize_java_text_block(content: str) -> str:
    """Approximates JEP 378's text-block incidental-whitespace algorithm:
    a line terminator immediately after the opening ``\"\"\"`` is not
    part of the content, every line's common leading whitespace (the
    LEAST indented non-blank line, closing delimiter's own line
    included per the JLS - LOW-3, round 7c, reviewer-3 delta on
    95d9cd8: an earlier version excluded the closing delimiter's line
    from this computation entirely, diverging from javac exactly when
    that line is indented LESS than every content line, now handled)
    is stripped, and each line's trailing whitespace is stripped - then
    the same escape sequences an ordinary literal supports are decoded.
    Not a byte-for-byte javac reimplementation (this adapter is coarse
    S1 evidence, not a full grammar), but no longer the raw, unindented-
    for-nothing source substring round 6 published."""
    if content.startswith("\r\n"):
        content = content[2:]
    elif content.startswith("\n"):
        content = content[1:]
    lines = content.split("\n")
    # The closing delimiter sits on its OWN line exactly when this
    # content's last split segment is whitespace-only AND there is a
    # preceding line to end at (a single-line block has the delimiter
    # immediately after its own content, no separate line at all). That
    # line's OWN indentation counts toward the common minimum per the
    # JLS, even though it is blank - but the line itself is positional
    # only and never part of the published value.
    closing_line_is_delimiter_only = len(lines) > 1 and lines[-1].strip() == ""
    last_index = len(lines) - 1
    indents = [
        len(line) - len(line.lstrip(" \t"))
        for i, line in enumerate(lines)
        if line.strip() or (closing_line_is_delimiter_only and i == last_index)
    ]
    min_indent = min(indents) if indents else 0
    dedented = [line[min_indent:] if len(line) >= min_indent else line.lstrip(" \t") for line in lines]
    if closing_line_is_delimiter_only:
        dedented = dedented[:-1]
    stripped = [line.rstrip(" \t") for line in dedented]
    return _decode_java_string_escapes("\n".join(stripped))


def _java_string_literal_span(text: str, quote_pos: int) -> tuple[str, int] | None:
    """Given ``text[quote_pos] == '"'``, returns ``(decoded value, end
    position - one past the closing delimiter)`` - a triple-quoted text
    block's incidental whitespace stripped and its escapes decoded per
    JEP 378, or an ordinary literal's escapes decoded per JLS 3.10.7.
    ``None`` if the literal is never closed before end of file. The end
    position (fix round 10 MAJOR 1) is what lets a caller keep scanning
    for FURTHER elements of a Spring array-literal (``{"...", "..."}``)
    immediately after this one, rather than only ever recovering the
    first.

    B1 (fourth cold read, fix round 6): mirrors ``_strip_comments_and_
    strings``'s OWN boundary-finding logic exactly (same escaped-quote
    skip, same triple-quote handling) - the same rule that decides where
    a string ends for SANITIZATION purposes now also decides what its
    content IS for route-value extraction, closing the escaped-quote and
    text-block members of the nested-paren truncation family as a side
    effect of reusing one mechanism, not as separate fixes.

    Minor 6 (fifth cold read, fix round 7): round 6 returned this RAW
    source substring - a text block's own leading newline/indentation,
    and an ordinary literal's escape sequences (`\\"` published as two
    literal characters, backslash included, never as the one character
    it represents) - as the published route AND its stable ID. Both
    branches now decode per Java's own semantics before returning."""
    n = len(text)
    if text[quote_pos:quote_pos + 3] == '"""':
        content_start = quote_pos + 3
        close = text.find('"""', content_start)
        if close == -1:
            return None
        return _normalize_java_text_block(text[content_start:close]), close + 3
    end = quote_pos + 1
    while end < n and text[end] != '"':
        end += 2 if text[end] == "\\" and end + 1 < n else 1
    if end >= n:
        return None
    return _decode_java_string_escapes(text[quote_pos + 1:end]), end + 1


def _java_string_literal_content(text: str, quote_pos: int) -> str | None:
    span = _java_string_literal_span(text, quote_pos)
    return span[0] if span is not None else None


def _route_paths(
    sanitized: str, original: str, group_start: int, group_end: int,
) -> list[str] | None:
    """Recover the annotation's literal path/value string(s), in
    declaration order. LOCATES the attribute (by name, or the leading
    positional literal) against the SANITIZED segment - comments and
    string content are already blanked there, so a commented-out
    ``value = "..."`` cannot match at all, its letters erased along with
    the rest of the comment. Then reads the literal content(s) from the
    ORIGINAL text, starting exactly at that match's end position
    (sanitization preserves length/position exactly).

    Three-state return (fix round 11, seventh cold read BLOCKER part 2 -
    the fail-safe for unrecoverable values): a non-empty list is one or
    more recovered literals; ``[]`` means the annotation genuinely
    carries NO value/path attribute and no positional literal at all
    (Spring's own "serves the prefix alone" semantics for a bare
    ``@GetMapping``) - legitimate, composes with an empty method value;
    ``None`` means a value expression IS present but could not be
    recovered as a literal (a constant reference, a concatenation, ...) -
    the caller must treat the whole route as UNKNOWN, never compose
    against an implicit empty value or publish a partial/fabricated
    guess. Distinguishing these is exactly the blind spot a plain "no
    mapping" test cannot see: genuinely-absent and can't-read must never
    collapse to the same outcome.

    MAJOR 1 (sixth cold read, fix round 10): a multi-value route array
    (``@GetMapping({"/list", "/all"})``) used to publish only its FIRST
    element - ``/all`` silently dropped, on a run reporting complete/
    valid. These are declared, trivially-present values (the multi-
    entry-point machinery already exists for the method/path fan-out
    below); every element is now recovered.

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
    list. Each result is length-bounded (invariant 3), never an
    unbounded raw excerpt.

    Minor 5 (fifth cold read, fix round 7): round 6 took only the FIRST
    named-attribute match and required it to be immediately followed by
    a literal - a non-literal attribute occurrence (e.g. an unrelated
    identifier the sanitized text still matches `value|path` against)
    ahead of the real, literal-valued one made the whole function give
    up where it previously recovered a route. Every named match is now
    tried in turn, falling back to the positional literal only once none
    of them panned out - a strictly WIDER recovery than round 6's, never
    narrower."""
    sanitized_segment = sanitized[group_start:group_end]
    unrecoverable = False
    for match in _ROUTE_NAMED_ATTR_RE.finditer(sanitized_segment):
        values = _route_literal_list_at(original, group_start + match.end())
        if values is None:
            unrecoverable = True
            continue
        if values:
            return values
    if not unrecoverable:
        positional_match = _ROUTE_POSITIONAL_ANCHOR_RE.match(sanitized_segment)
        if positional_match is not None:
            # CR9-3: the slot right after "(" may belong to a different
            # NAMED attribute (produces=, consumes=, ...), not an
            # attempted positional literal at all - never treat that as
            # an unreadable value; the annotation is simply valueless.
            leads_with_other_named_attr = _ROUTE_LEADING_NAMED_ATTR_RE.match(
                sanitized_segment, positional_match.end()) is not None
            if not leads_with_other_named_attr:
                values = _route_literal_list_at(original, group_start + positional_match.end())
                if values is None:
                    unrecoverable = True
                elif values:
                    return values
    return None if unrecoverable else []


def _value_terminates_at(original: str, pos: int) -> bool:
    """Whether ``pos`` (skipping whitespace) sits at a legitimate
    boundary for the value just recovered - the next named attribute's
    comma, or the annotation's own closing paren. Anything else (a `+`,
    another token) means what was just read is only the FIRST fragment
    of a larger expression (e.g. string concatenation) - fix round 11:
    silently returning that first fragment as if it were the whole value
    published a FABRICATED path worse than a bare omission."""
    n = len(original)
    while pos < n and original[pos].isspace():
        pos += 1
    return pos >= n or original[pos] in ",)"


def _route_literal_list_at(original: str, anchor: int) -> list[str] | None:
    """Every string literal value at ``anchor``: a bare literal
    (``"..."``), or, when Spring's own array-literal shorthand is used
    for a multi-value ``value``/``path``/positional attribute
    (``{"...", "..."}``), EVERY element in declaration order (fix round
    10 MAJOR 1). Returns ``[]`` when nothing at all sits here (a
    genuinely valueless annotation); ``None`` (fix round 11) when
    something sits here but is not a clean literal or literal array -
    a constant reference, a concatenation, or an array containing any
    non-literal element - never silently truncated to whichever leading
    literal fragment happened to parse."""
    n = len(original)
    pos = anchor
    while pos < n and original[pos].isspace():
        pos += 1
    if pos >= n or original[pos] in ",)":
        return []
    if original[pos] == "{":
        values: list[str] = []
        pos += 1
        while pos < n:
            while pos < n and (original[pos].isspace() or original[pos] == ","):
                pos += 1
            if pos < n and original[pos] == "}":
                return values
            if pos >= n or original[pos] != '"':
                return None
            span = _java_string_literal_span(original, pos)
            if span is None:
                return None
            content, pos = span
            values.append(_bounded_route_target(content))
        return None
    if original[pos] != '"':
        return None
    span = _java_string_literal_span(original, pos)
    if span is None:
        return None
    content, end = span
    if not _value_terminates_at(original, end):
        return None
    return [_bounded_route_target(content)]


def _bounded_route_target(value: str) -> str:
    if len(value) <= _MAX_ROUTE_TARGET_LENGTH:
        return value
    return value[:_MAX_ROUTE_TARGET_LENGTH] + "...(truncated)"


#: Fix round 10 (structural order, sixth cold read BLOCKER - THIRD
#: recurrence of this class: round 6 M5, round 7 B1, now this): every
#: prior version walked FORWARD from an annotation across an ENUMERATED
#: trivia grammar (a fixed modifier-keyword set, a bare-annotation-name
#: regex) - each fix enumerated the shapes it had just been shown, and
#: the next ordinary one (a FULLY-QUALIFIED stacked annotation like
#: ``@org.springframework.stereotype.Component`` - the dot stops a
#: ``@\w+`` match; a ``non-sealed`` modifier - the hyphen stops an
#: identifier match) fell outside the enumeration, silently returning
#: "not class-level" and resurrecting the phantom prefix-as-route bug.
#:
#: Inverted here: anchor BACKWARD from each extracted type header (the
#: header finder - _TYPE_NAME_ANCHOR_RE plus the depth-aware clause scan
#: - is already proven robust; a 19-shape battery survived it) rather
#: than forward from an annotation. Nothing needs to be enumerated: per
#: the JLS, ONLY whitespace, modifiers, and annotations can legally
#: precede a type header, so ANY identifier-shaped token here (including
#: a hyphenated compound like ``non-sealed`` - never a specific
#: allow-listed keyword) and ANY dotted annotation name (simple or
#: fully-qualified) are accepted - retiring the enumeration class
#: permanently instead of widening it once more.
_DOTTED_IDENTIFIER_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$."
)
_MODIFIER_TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$-"
)


def _backward_dotted_identifier_start(sanitized: str, end: int) -> int | None:
    """The start of a (possibly dotted/fully-qualified) identifier ending
    EXACTLY at ``end``, or ``None`` if ``end`` is not the end of one."""
    if end == 0:
        return None
    if sanitized[end - 1] not in _DOTTED_IDENTIFIER_CHARS or sanitized[end - 1] == ".":
        return None
    start = end
    while start > 0 and sanitized[start - 1] in _DOTTED_IDENTIFIER_CHARS:
        start -= 1
    while start < end and sanitized[start] == ".":
        start += 1
    return start


def _backward_modifier_token_start(sanitized: str, end: int) -> int | None:
    """The start of a bare modifier-SHAPED token (ANY identifier,
    including a hyphenated compound like ``non-sealed``) ending EXACTLY
    at ``end`` - never checked against a specific keyword set (see
    module note above)."""
    if end == 0:
        return None
    ch = sanitized[end - 1]
    if not (ch.isalnum() or ch in "_$"):
        return None
    start = end
    while start > 0 and sanitized[start - 1] in _MODIFIER_TOKEN_CHARS:
        start -= 1
    while start < end and (sanitized[start] == "-" or sanitized[start].isdigit()):
        start += 1
    return start if start < end else None


def _preceding_declaration_start(sanitized: str, header_start: int) -> int:
    """Walks BACKWARD from a type header's own start position through
    whitespace, modifier-shaped tokens, and stacked annotations (each
    with its own optional, depth-aware, argument list via
    :func:`_matching_open_paren`) - returns the leftmost position such
    that ``[return value, header_start)`` is PURE declaration trivia.
    Stops (returns the current position) the instant something else is
    found, exactly the same safe non-guess every prior version made for
    an unrecognized shape - the difference is what counts as
    recognized: everything the JLS actually allows here, not an
    enumerated subset of it."""
    pos = header_start
    while pos > 0:
        p = pos
        while p > 0 and sanitized[p - 1].isspace():
            p -= 1
        if p == 0:
            return 0
        if sanitized[p - 1] == ")":
            open_pos = _matching_open_paren(sanitized, p - 1)
            if open_pos is None:
                return pos
            name_end = open_pos
            while name_end > 0 and sanitized[name_end - 1].isspace():
                name_end -= 1
            name_start = _backward_dotted_identifier_start(sanitized, name_end)
            if name_start is None or name_start == 0 or sanitized[name_start - 1] != "@":
                return pos
            pos = name_start - 1
            continue
        name_start = _backward_dotted_identifier_start(sanitized, p)
        if name_start is not None and name_start > 0 and sanitized[name_start - 1] == "@":
            pos = name_start - 1
            continue
        modifier_start = _backward_modifier_token_start(sanitized, p)
        if modifier_start is not None:
            pos = modifier_start
            continue
        return pos
    return 0


def _class_header_associations(
    sanitized: str,
    types: list[tuple[str, str, str, int, str | None, str | None, int]],
) -> list[tuple[int, int, str]]:
    """``[(declaration_start, header_start, qualified_name), ...]`` for
    every declared type - the backward-anchored trivia span computed
    ONCE per header, up front, rather than re-derived per annotation."""
    return [
        (_preceding_declaration_start(sanitized, header_start), header_start, qualified)
        for qualified, _s, _c, header_start, _e, _i, _end in types
    ]


def _class_level_route_target(
    ann_start: int, associations: list[tuple[int, int, str]],
) -> str | None:
    """If ``ann_start`` (a route annotation's OWN start position) falls
    inside some type header's backward-anchored declaration-trivia span,
    returns that type's qualified name. ``None`` means this route
    annotation is not immediately, purely-trivially, attached to any
    known type header - it is either genuinely method-level, or (fix
    round 10 fail-safe) an association this adapter cannot confidently
    establish, which the caller must treat as absence, never as a guess."""
    for declaration_start, header_start, qualified in associations:
        if declaration_start <= ann_start < header_start:
            return qualified
    return None


def _normalize_route_leading_slash(path: str) -> str:
    """A published route target is always an absolute path. Shared by
    :func:`_compose_route_path` (the prefix half) and its caller (a
    STANDALONE method route with no class-level prefix at all) - LOW-2
    (round 7c, reviewer-3 delta on 95d9cd8): this normalization used to
    live ONLY inside composition, so a bare method-only route lacking
    its own leading ``/`` published exactly as written while an
    otherwise-identical route that happened to have a (even empty)
    class-level prefix got normalized - two spellings for the same
    served path, depending on something the route itself has no say
    over. One normalization point now covers both shapes."""
    if not path or path.startswith("/"):
        return path
    return "/" + path


def _compose_route_path(prefix: str, path: str) -> str:
    """Spring's OWN declared composition semantics for a class-level
    ``@RequestMapping`` prefix plus a method-level route value - not
    inference (M5, fourth cold read, fix round 6).

    M5 composition notes (fifth cold read, fix round 7): a class prefix
    lacking its own leading ``/`` is normalized to one (a route target is
    always an absolute path), and an EMPTY method-level value (a
    valueless method annotation, composed by the caller as ``""``)
    yields the prefix alone - never a spurious trailing ``/``."""
    prefix_part = _normalize_route_leading_slash(prefix.rstrip("/"))
    path_part = path.lstrip("/")
    if not path_part:
        return prefix_part
    return f"{prefix_part}/{path_part}"


def parse_java_source(relative_path: str, text: str) -> JavaFileResult:
    """Parse one ``.java`` file's TEXT (already read by the sanitized
    worker - this function never touches the filesystem itself)."""
    sanitized = _strip_comments_and_strings(text)
    newline_offsets = _newline_offsets(sanitized)
    package_match = _PACKAGE_RE.search(sanitized)
    package = package_match.group(1) if package_match else None

    imports = []
    # FIX ROUND 13 (ninth cold read, CR9-5): stores (target, is_static) -
    # a plain import binds its simple name to a TYPE's own FQN; a static
    # import binds it to a MEMBER path (Type.MEMBER) whose type prefix,
    # not the full path, is what the invoke qualifier below must resolve
    # against (see the elif branch) - conflating the two previously let
    # a static-imported member used as a bare qualifier (e.g. a
    # constant field used like ``LOGGER.info(...)``) publish resolved/
    # EXTERNAL against the unsplit member path, contradicting the SAME
    # import's own edge (which already strips the member correctly).
    import_simple_names: dict[str, tuple[str, bool]] = {}
    for match in _IMPORT_RE.finditer(sanitized):
        is_static = bool(match.group(1))
        target = match.group(2)
        imports.append((target, is_static, _line_at(newline_offsets, match.start())))
        if not target.endswith(".*"):
            import_simple_names[target.rsplit(".", 1)[-1]] = (target, is_static)
    # FIX ROUND 14 (CR10-7): corroborating evidence for a name-suffix-
    # only test classification - a test-framework import anywhere in
    # THIS file (imports are file-scoped, same reasoning as CR10-1).
    has_test_framework_evidence = any(
        target.startswith(prefix)
        for target, _is_static, _line in imports
        for prefix in _TEST_FRAMEWORK_IMPORT_PREFIXES
    )

    types = _extract_types(sanitized, package)
    local_simple_names = {simple for _, simple, *_ in types}
    primary_qualified = types[0][0] if types else (package or relative_path)
    # FIX ROUND 14 (tenth cold read, CR10-1 MAJOR): an ``import`` is a
    # FILE-scoped Java fact (every type declared in the file sees every
    # import, regardless of which one actually uses it) - publishing it
    # against ``primary_qualified`` (the FIRST declared type) fabricated
    # a type-scoped claim: in a public-class-plus-package-private-helper
    # file, the FIRST class was credited with the helper's own import
    # (a false edge), and the helper itself published no edges at all,
    # letting readiness stamp it satisfied/no_declared_dependencies with
    # zero real evidence - exactly the un-evidenced positive the
    # readiness policy refuses everywhere else. Never a real type's own
    # qualified name (relative_path's "/" and ".java" can never appear
    # in a dotted Java qualified name), so dependencies_artifact.py's
    # existing exact-lookup-or-file-unit fallback (``by_qualified_name.
    # get(...) or file_unit_id_by_path[path]``) routes every import edge
    # to the FILE unit - already addressable, and the one unit an import
    # is honestly a fact ABOUT - rather than any specific declared type.
    file_scope_qualified = f"{relative_path}#file"

    units = [
        JavaUnitClaim(
            relative_path=relative_path,
            qualified_name=qualified,
            simple_name=simple,
            line=_line_at(newline_offsets, brace_pos),
            classification=_classify(
                relative_path, simple, has_test_framework_evidence=has_test_framework_evidence),
        )
        for qualified, simple, _container, brace_pos, _extends, _implements, _end in types
    ]

    edges: list[JavaEdgeClaim] = []
    entry_points: list[JavaEntryPointClaim] = []
    problems: list[JavaAdapterProblem] = []

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
            from_qualified_name=file_scope_qualified, relation="import", target=target,
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
        # FIX ROUND 14 (CR10-7 MINOR, wrong-data): a bare name-suffix
        # match is not corroborating evidence on its own (see
        # _TEST_NAME_SUFFIX's own comment) - an ordinary production
        # class named e.g. AUDIT matched "IT" and published a
        # FABRICATED test edge to a nonexistent stripped-suffix target
        # ("AUD"). Requires the SAME corroboration _classify itself now
        # requires - a test source root (checked directly here, since a
        # nested type's own qualified/simple name carries no path
        # information) OR a test-framework import in this file.
        if _TEST_NAME_SUFFIX.search(simple) and (
            has_test_framework_evidence or _TEST_PATH_SEGMENT.search(relative_path.replace("\\", "/"))
        ):
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
            # FIX ROUND 13 (CR9-5): a static import's bare simple name
            # binds to a MEMBER path (Type.MEMBER) - strip the trailing
            # member segment to get the owning class's own FQN, the
            # exact same normalization the import edge itself already
            # applies (N5, fix round 6) - never the raw member path,
            # which cannot match any type's own qualified name.
            imported_target, is_static_import = import_simple_names[qualifier]
            target_kind = "internal_exact_or_external"
            qualifier = (
                imported_target.rsplit(".", 1)[0] if is_static_import else imported_target
            )
        else:
            # M12 (cold-read, PR-B fix round 3): neither locally declared
            # nor import-recognized - could be a genuine same-package
            # sibling (Java needs no import for that), but could equally
            # be a JDK/library type this extractor has no import evidence
            # for.
            #
            # FIX ROUND 14 (tenth cold read, CR10-2 MAJOR): this used to
            # be a NARROWER kind ("internal_unqualified_call_candidate",
            # exact-qualified-match only, no fallback at all) specifically
            # to avoid the GLOBAL simple-name matcher round 12 later
            # closed for inherit/test - so `Caller extends Util` (same
            # package, no import) resolved via the ladder while `Caller`'s
            # OWN `Util.go()` call, the identical relationship, stayed
            # UNRESOLVED in the same run: two contradictory facts about
            # one dependency in one artifact, and virtually every ordinary
            # same-package call in a normal multi-file package landed
            # unresolved. Round 12 already closed the door this kind
            # existed to guard - ``_resolve_internal_candidate`` no longer
            # has a dangerous global bare-name fallback (a single same-
            # named candidate anywhere in the scan no longer auto-
            # resolves) - so invoke's bare qualifier now shares the exact
            # SAME ladder inherit/test already use: same-file declaration,
            # then this file's own import, then same-package sibling,
            # else unresolved (or ambiguous for a genuine same-simple-name
            # collision) - one resolution discipline for all three
            # relations, never three copies of it.
            target_kind = "internal_candidate"
        edges.append(JavaEdgeClaim(
            from_qualified_name=_enclosing_qualified_name(match.start(), types, primary_qualified),
            relation="invoke", target=qualifier,
            target_kind=target_kind, evidence_class="extracted",
            line=_line_at(newline_offsets, match.start()), phase="runtime",
        ))

    def _route_annotation_span(match: re.Match) -> tuple[int, list[str] | None, list[str]]:
        # N10 (third cold read, fix round 5): find the annotation's own
        # argument-list parens by tracking nesting depth (below), rather
        # than a regex that stopped at the FIRST close-paren anywhere in
        # the argument list - see _matching_close_paren's docstring for
        # the truncation this replaces. Returns (position right after
        # this annotation's own span, path(s), explicit method(s)) - the
        # position is what a class-level check must resume from, never
        # match.end() (which sits BEFORE this annotation's own
        # arguments, not after them).
        arg_pos = match.end()
        while arg_pos < len(sanitized) and sanitized[arg_pos].isspace():
            arg_pos += 1
        if arg_pos < len(sanitized) and sanitized[arg_pos] == "(":
            close_pos = _matching_close_paren(sanitized, arg_pos)
            if close_pos is not None:
                return (
                    close_pos + 1,
                    _route_paths(sanitized, text, arg_pos, close_pos + 1),
                    _route_method_attributes(sanitized[arg_pos:close_pos + 1]),
                )
        return match.end(), [], []

    # M5 (fourth cold read, fix round 6): a class-level @RequestMapping is
    # a PREFIX for every method-level route inside that class - Spring's
    # own declared composition semantics, not inference (composing them
    # was previously never attempted at all: a class-level "/api/orders"
    # plus a method-level "/list" published as two independent routes,
    # the method's own published value "/list" a bare FRAGMENT of the
    # actually-served "/api/orders/list" in the field named for the whole
    # route). First pass: find every class-level route annotation (one
    # sitting directly on a type, not a method - see
    # _class_level_route_target) and record its literal prefix(es), only
    # when at least one was itself confidently extracted.
    #
    # Fix round 10 (structural order): association is now backward-
    # anchored from each type header (computed ONCE, here), and a route
    # annotation's own START position - not the position AFTER it, which
    # a class-level check never actually needed to resume walking from -
    # is what gets tested against it.
    class_header_associations = _class_header_associations(sanitized, types)
    class_route_prefix: dict[str, list[str]] = {}
    # Fix round 11 (seventh cold read BLOCKER part 2 - the fail-safe for
    # unrecoverable values): a class-level route annotation whose OWN
    # value could not be recovered as a literal (a constant reference, a
    # concatenation, ...) must never silently compose against an
    # implicit EMPTY prefix - every method-level route inside that class
    # is UNKNOWN, not a bare fragment of whatever the real prefix is.
    # Tracked separately from "no entry at all" (a genuinely valueless
    # class-level annotation, Spring's own legitimate "no prefix"
    # semantics) - checked FIRST in pass two, below, so it wins even if
    # some OTHER class-level annotation on the same type also happened
    # to register a real prefix.
    class_route_prefix_unrecoverable: set[str] = set()
    for match in _ROUTE_ANNOTATION_RE.finditer(sanitized):
        _span_end, paths, _explicit_methods = _route_annotation_span(match)
        target_type = _class_level_route_target(match.start(), class_header_associations)
        if target_type is None:
            continue
        if paths is None:
            class_route_prefix_unrecoverable.add(target_type)
        elif paths:
            class_route_prefix[target_type] = paths

    for match in _ROUTE_ANNOTATION_RE.finditer(sanitized):
        line = _line_at(newline_offsets, match.start())
        enclosing = _enclosing_qualified_name(match.start(), types, primary_qualified)
        _span_end, paths, explicit_methods = _route_annotation_span(match)
        class_target = _class_level_route_target(match.start(), class_header_associations)
        if class_target is not None:
            # A bare class-level annotation with no method-level mapping
            # inside that class represents no invocable route on its own
            # - already captured as a prefix above, never its own edge/
            # entry point.
            continue
        if class_target is None and types and not _position_inside_any_type_body(match.start(), types):
            # FAIL-SAFE (fix round 10, the class-closer): this route
            # annotation sits OUTSIDE every extracted type's own brace
            # body - a genuine method-level route annotation always
            # lives INSIDE its class's braces, so a position outside
            # every one of them means this annotation precedes a type
            # declaration (or something unforeseen) that backward
            # anchoring could not confidently associate. Inability to
            # associate used to fail toward publishing the annotation's
            # own literal value as if it were a complete, invocable
            # route, attributed to the wrong (file-level) owner - wrong
            # data, three rounds running. It now fails toward visible
            # absence: suppress the claim, record why, never guess.
            #
            # Round 10c: an ANNOTATION-TYPE declaration (`@interface` -
            # Spring's own composed-annotation idiom) is now a
            # first-class extracted header whose span starts at its own
            # `@` (see _extract_types), so a route annotation stacked on
            # one associates normally via class_header_associations
            # above and never reaches this branch at all - no special
            # case needed here to keep in step with that one.
            problems.append(JavaAdapterProblem(
                reason_code="route_annotation_unassociated",
                detail=f"a class-level-looking route annotation at line {line} could not be "
                       "confidently associated with any declared type - suppressed rather "
                       "than published as a route",
            ))
            continue
        if paths is None:
            # FAIL-SAFE (fix round 11, seventh cold read BLOCKER part 2):
            # this route annotation's OWN value could not be recovered
            # as a literal - a constant reference, a concatenation
            # (silently taking its FIRST literal fragment would publish
            # a path the application never serves - a fabrication worse
            # than a bare fragment), or any other non-literal expression.
            # Never compose against an implicit empty value; suppress
            # and record why.
            problems.append(JavaAdapterProblem(
                reason_code="route_value_unrecoverable",
                detail=f"a route annotation at line {line} has a value that could not be "
                       "recovered as a literal - suppressed rather than published with a "
                       "guessed or partial value",
            ))
            continue
        if enclosing in class_route_prefix_unrecoverable:
            # FAIL-SAFE (fix round 11): the enclosing class's OWN route
            # prefix could not be recovered - composing this method's
            # value against an implicit empty prefix would publish a
            # bare FRAGMENT as if it were the complete served path.
            problems.append(JavaAdapterProblem(
                reason_code="route_value_unrecoverable",
                detail=f"a route annotation at line {line} is inside a class whose own route "
                       "prefix could not be recovered as a literal - suppressed rather than "
                       "published as an incomplete fragment",
            ))
            continue
        prefixes = class_route_prefix.get(enclosing)
        if prefixes:
            if paths:
                composed = [_compose_route_path(prefix, p) for prefix in prefixes for p in paths]
            else:
                # M5 composition note (fifth cold read, fix round 7): a
                # valueless method annotation (bare ``@GetMapping``)
                # still serves the class's own prefix in Spring -
                # composing with an empty method value (never skipping
                # composition just because there is no method-level
                # literal) instead of falling through to the synthetic
                # fallback below and silently losing the prefix
                # entirely.
                composed = [_compose_route_path(prefix, "") for prefix in prefixes]
        elif paths:
            # LOW-2 (round 7c): the same leading-slash normalization
            # _compose_route_path applies to a class prefix, applied
            # here too - a STANDALONE method route (no class-level
            # prefix at all) must not publish a different spelling of
            # the same served path just because it lacked one.
            composed = [_normalize_route_leading_slash(p) for p in paths]
        else:
            composed = []
        # N2 (fifth cold read, fix round 8): a verb-specific annotation's
        # own implied method (GetMapping -> GET, ...) always wins when
        # known; a plain @RequestMapping has none of its own, so its
        # explicit method=RequestMethod.X attribute(s) (if present) are
        # what distinguishes it from another @RequestMapping on the
        # same path - without this, two such routes collapse into one
        # coalesced entry point (round 5's M-5), silently losing that
        # they are two different handlers.
        #
        # MAJOR 1/N4 (sixth cold read, fix round 10): a multi-value
        # route array, and a multi-value ``method = {...}`` attribute,
        # each publish only their FIRST element before - every declared
        # combination (path x method) is now its own entry point, the
        # multi-entry-point machinery already existing for this fan-out.
        verb = _ROUTE_METHOD_BY_ANNOTATION.get(match.group(1))
        methods: list[str | None] = [verb] if verb else (explicit_methods or [None])
        if composed:
            targets = [
                f"{m} {p}" if m else p
                for m in methods
                for p in composed
            ]
        else:
            targets = [f"{enclosing}#{match.group(1)}"]
        for target in targets:
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
    #
    # FIX ROUND 13 (CR9-2): de-enumerated - see _MAIN_HEADER_RE's own
    # comment. "public" and "static" must both appear in the matched
    # modifier run, in any order; the regex alone only guarantees at
    # least one recognized modifier keyword is present.
    #
    # FIX ROUND 13c (reviewer-3's rejection of round 13b): every
    # "cli_main_unrecognized" problem is now ATTRIBUTED to the ONE
    # enclosing declared type it concerns (_enclosing_qualified_name -
    # the same machinery edges/entry points already use), never
    # broadcast file-wide - a 3-class file where only the third has a
    # main-like method must never flag the other two. And the MILDER
    # fix: a recovered parameter list is now classified into three
    # outcomes, not two - (1) the exact String[]/varargs shape (subject
    # to the public+static check, as before); (2) a JLS-CERTAIN wrong
    # shape (wrong arity, or a base type that plainly is not String -
    # main(), main(int[]), main(String[], int) can NEVER be the JVM
    # entry point regardless of modifiers - silent, same as a
    # recognized-but-missing-modifier negative, no problem recorded);
    # (3) genuinely unrecognized (parens never close, or the shape
    # cannot be confidently placed in class 1 or 2) - THIS is the only
    # class that degrades to the unknown class-closer.
    for header_match in _MAIN_HEADER_RE.finditer(sanitized):
        line = _line_at(newline_offsets, header_match.start())
        enclosing = _enclosing_qualified_name(header_match.start(), types, primary_qualified)
        open_paren_pos = header_match.end() - 1
        close_pos = _matching_close_paren(sanitized, open_paren_pos)
        if close_pos is None:
            problems.append(JavaAdapterProblem(
                reason_code="cli_main_unrecognized",
                detail=f"a method literally named main returning void at line {line} did "
                       "not match any recognized public-static-void-main(String[]) "
                       "signature shape - no cli_main entry point published, but not "
                       "confidently absent either",
                qualified_name=enclosing,
            ))
            continue
        params = _split_top_level_commas(sanitized[header_match.end():close_pos])
        if len(params) == 1 and _MAIN_PARAM_FULL_RE.match(params[0]) is not None:
            modifiers = header_match.group(1).split()
            if "public" in modifiers and "static" in modifiers:
                entry_points.append(JavaEntryPointClaim(
                    qualified_name=enclosing, kind="cli_main", name="main",
                    line=line, evidence_class="extracted",
                ))
            # else: the exact JVM signature, recognized, confidently
            # missing a required modifier - a JLS-certain negative,
            # silent, never a problem.
            continue
        if len(params) != 1:
            # JLS-certain: the entry point takes EXACTLY one parameter -
            # main() and main(String[], int) can never qualify, whatever
            # the modifiers say.
            continue
        leading_type_match = _MAIN_PARAM_LEADING_TYPE_RE.match(params[0])
        if leading_type_match is not None and leading_type_match.group(1) not in _MAIN_STRING_TYPE_SPELLINGS:
            # JLS-certain: a single parameter whose base type is plainly
            # NOT String (main(int[] args), ...) can never be the entry
            # point, regardless of spelling or modifiers.
            continue
        # Either no base type could be determined at all, or it IS
        # String-shaped but not in any array/varargs form this adapter
        # recognizes - genuinely uncertain (the spelling-variant axis
        # every enumerated-recognizer lesson in this producer applies
        # to), never a silent negative.
        problems.append(JavaAdapterProblem(
            reason_code="cli_main_unrecognized",
            detail=f"a method literally named main returning void at line {line} did not "
                   "match any recognized public-static-void-main(String[]) signature "
                   "shape - no cli_main entry point published, but not confidently absent "
                   "either",
            qualified_name=enclosing,
        ))

    if not units:
        # BLOCKER (sixth cold read, fix round 9): route/entry-point
        # emission (and every other edge kind above) never consulted
        # whether this file actually yielded any types - a file that
        # degrades honestly (zero units published, the worker's own
        # no_types_extracted problem recorded, round 8's BLOCKER 1b)
        # STILL published the class-level route prefix as an invocable
        # route, the method value as the whole route, declared-class,
        # as stable entry-point IDs - every extraction loop above
        # (imports, invoke, route, cli_main) runs regardless of whether
        # ANY type was found, all falling back to the same SYNTHESIZED
        # owner (primary_qualified) that names no real unit at all.
        # Proven with valid, unicode-escaped-brace Java source: the
        # LANGUAGE decodes \uXXXX escapes before lexing (so real javac
        # compiles it fine); this adapter's sanitizer does not, so its
        # own brace-matching never even sees the type's body, while
        # every other loop keeps running on the surrounding text
        # regardless. Under-claim over guess: when a file yields no
        # types, suppress every edge/entry-point claim from it - the
        # problem record already carries visibility; a synthesized
        # owner would only launder unattributable claims into the
        # published artifacts. The suppression lives here, at the one
        # place this function actually returns its result, not as a
        # filter a future caller could forget to apply.
        return JavaFileResult(units=[], edges=[], entry_points=[], problems=[])
    return JavaFileResult(units=units, edges=edges, entry_points=entry_points, problems=problems)


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

#: M1 (seventh cold read, fix round 11): a bare open/close XML tag
#: (attributes ignored - this adapter's own bar is "a handful of flat
#: structural tags", not a general XML parser); a trailing ``/`` before
#: ``>`` marks a self-closing tag, which never opens a body at all.
_XML_TAG_RE = re.compile(r"<(/?)([A-Za-z][\w.-]*)\b[^>]*?(/?)>")
#: Every ``<dependencies>...</dependencies>`` element, regardless of
#: nesting context - non-greedy, mirroring ``_DEPENDENCY_BLOCK_RE``'s own
#: same-shaped non-nesting assumption (Maven's own schema never nests
#: one ``<dependencies>`` inside another).
_DEPENDENCIES_ELEMENT_RE = re.compile(r"<dependencies>(.*?)</dependencies>", re.DOTALL)


def _enclosing_tag_stack(sanitized: str, before: int) -> list[str]:
    """The full ancestor tag-name stack open at position ``before`` - a
    plain forward tag scan up to (not including) that position. Depth-
    aware element-context tracking, the same technique
    ``_matching_close_paren`` already owns for a different bracket
    family, applied here to XML tags instead of parens."""
    stack: list[str] = []
    for tag_match in _XML_TAG_RE.finditer(sanitized, 0, before):
        if tag_match.group(3) == "/":
            continue  # self-closing: never opens a body, nothing to push/pop
        name = tag_match.group(2)
        if tag_match.group(1) == "/":
            if stack and stack[-1] == name:
                stack.pop()
        else:
            stack.append(name)
    return stack


def _module_own_dependency_blocks(sanitized: str) -> list[re.Match]:
    """M1 (seventh cold read BLOCKER, wrong-data): ``parse_maven_pom``
    used to match every ``<dependency>`` block ANYWHERE in the file -
    ``<dependencyManagement>`` (transitive/BOM-style declarations, NOT
    direct dependencies of this module - a parent/BOM pom can carry
    dozens), a ``<profile>``'s own conditionally-active dependencies,
    and a ``<plugin>``'s own build-tool dependency all published as
    undifferentiated direct declared build edges, contradicting this
    function's own docstring promise of "direct" dependencies.

    Scoped to the module's own TOP-LEVEL ``<dependencies>`` element - a
    direct child of ``<project>``, never one nested inside
    ``<dependencyManagement>``/a ``<profile>``/a ``<plugin>`` - via
    element-context tracking (``_enclosing_tag_stack``), the same
    depth-aware technique this adapter already owns for a different
    bracket family.

    Named decision (judged, not silently decided): plugin- and profile-
    scoped dependencies are EXCLUDED from this slice's direct-dependency
    edges, not differentiated with a marker - they are not direct
    dependencies of the module by Maven's own semantics (a profile's
    dependencies are conditionally active; a plugin's dependencies are
    the BUILD TOOL's own, not the module's), and inventing a phase/marker
    for them would imply a supported, evidenced distinction this slice
    does not actually make. Honest v1: excluded, named here."""
    for deps_match in _DEPENDENCIES_ELEMENT_RE.finditer(sanitized):
        if _enclosing_tag_stack(sanitized, deps_match.start()) != ["project"]:
            continue
        yield from _DEPENDENCY_BLOCK_RE.finditer(
            sanitized, deps_match.start(1), deps_match.end(1))


def _count_profile_scoped_dependencies(sanitized: str) -> int:
    """Round 11c (reviewer-3 delta on round 11b, VEHICLE CHANGE): every
    ``<dependency>`` block inside a ``<profile>``'s own ``<dependencies>``
    element, counted. Round 11's own M1 fix excludes profile-scoped
    dependencies from the direct-dependency edges - a profile can be
    ACTIVE BY DEFAULT (``activeByDefault``/JDK/property/OS activation),
    so this may be a potentially LIVE dependency of the module, not a
    cost-free exclusion the way managed/plugin scoping is.

    Round 11b published this as a run-degrading PROBLEM - but Maven
    profiles are common enough in real repos that a large share of them
    would scan degraded PERMANENTLY over a DECLARED, deliberate scope
    limitation - not the same kind of thing as an unreadable
    ``.gitmodules`` or an unrecoverable route value, and diluting what
    "degraded" means by putting both in the same bucket. Surfaced
    instead as a named exclusion COUNT (the same idiom
    ``scan.json``'s own ``exclusions`` map already uses for discovery-
    level categories) - visible without touching run status at all."""
    count = 0
    for deps_match in _DEPENDENCIES_ELEMENT_RE.finditer(sanitized):
        if _enclosing_tag_stack(sanitized, deps_match.start()) != ["project", "profiles", "profile"]:
            continue
        count += sum(
            1 for _ in _DEPENDENCY_BLOCK_RE.finditer(
                sanitized, deps_match.start(1), deps_match.end(1))
        )
    return count


def parse_maven_pom(
    relative_path: str, text: str,
) -> tuple[list[JavaEdgeClaim], int]:
    """Direct-dependency ``build`` edges from a ``pom.xml``'s module-own,
    top-level ``<dependency>`` blocks (see ``_module_own_dependency_
    blocks`` for the M1/round-11 scoping fix and its named plugin/
    profile decision). Plain regex over a small, well-known XML shape -
    no XML parser (and its entity-expansion surface) needed for a
    handful of flat child elements. Returns ``(edges,
    profile_scoped_dependency_count)`` - see
    ``_count_profile_scoped_dependencies`` for round 11c's exclusion-
    count vehicle (managed/plugin scoping needs no count at all: judged
    not-omissions, cost-free, never the module's own dependency graph).

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
    for match in _module_own_dependency_blocks(sanitized):
        block = match.group(1)
        group_match = _DEPENDENCY_GROUP_ID_RE.search(block)
        artifact_match = _DEPENDENCY_ARTIFACT_ID_RE.search(block)
        if group_match is None or artifact_match is None:
            continue
        # CR9-6 (ninth cold read, judged, completeness): a pom's own
        # groupId/artifactId published VERBATIM, UNBOUNDED (a hostile or
        # merely enormous pom - a 5000-char fixture - published whole),
        # while every Java route target is already length-bounded
        # (invariant 3) - routed through the same per-field discipline.
        group_id = _bounded_route_target(group_match.group(1).strip())
        artifact_id = _bounded_route_target(artifact_match.group(1).strip())
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
    return edges, _count_profile_scoped_dependencies(sanitized)


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
        servlet_name = match.group(1).strip()
        # CR9-6 (ninth cold read, judged, completeness): same per-field
        # bounding discipline as the pom producer above and every Java
        # route target - a url-pattern published verbatim, unbounded.
        url_pattern = _bounded_route_target(match.group(2).strip())
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
        "problems": [asdict(p) for p in result.problems],
    }


def file_result_from_json(payload: dict[str, Any]) -> JavaFileResult:
    return JavaFileResult(
        units=[JavaUnitClaim(**u) for u in payload["units"]],
        edges=[JavaEdgeClaim(**e) for e in payload["edges"]],
        entry_points=[JavaEntryPointClaim(**p) for p in payload["entry_points"]],
        problems=[JavaAdapterProblem(**p) for p in payload.get("problems", [])],
    )
