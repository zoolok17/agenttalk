# A strict plan sidecar for the Planned lane

Status: proposed, revision 2 (after design review). Date: 2026-10-02.

## In plain words

This is a proposal, not something you can run yet. It is about how the work
board learns which work is *planned* but not started.

Today, planned work lives only inside a plan's Markdown file - the same
file a person reads. Teaching a program to find the work-items table inside
that free-form prose turned out to be unsafe: across eight review rounds
(PR #264), every fix closed one way to hide or fake a row, and every round
a reviewer found a new one.

This document proposes to stop trying. Plans stay written in Markdown, for
people to read. The few facts the board needs - which work items, in what
order, for whom - move into a second, small, strict file next to it: a
sidecar in JSON, checked against a published schema before anything is
read from it.

**Revision 2** tightens this proposal after its first design review found
the sidecar idea sound but its details not yet safe to build: a sidecar
can still be decoded ambiguously (JSON itself has a few quiet traps), two
honestly-written sidecars can still collide on the same plan id, and a
plain "missing file" could still be read the same as "damaged file" by
code this design was going to reuse as-is. This revision adds a strict
decoding contract, an explicit identity and guarded-replacement rule, and
is honest about which parts of the parked PR #264 branch this work would
actually inherit versus still needs to port or fix. Six open questions
from revision 1 are now written in as decisions (§0).

What you will notice, once this is built: nothing changes in how a plan
reads as a document. A new file sits next to it (`<plan>.items.json`), and
`agenttalk board import-plan` reads that file instead of the Markdown.

What you need to do, right now: nothing - review this design. No code
changes are proposed yet.

---

## 0. Decisions adopted from the design review

The first design review asked six open questions; the lead adopted the
reviewer's recommendations on all six as written. These are now decisions,
not questions:

1. **Size.** A separately named `MAX_SIDECAR_BYTES`, initially 512 KiB (the
   same number `MAX_FILE_BYTES` already uses for the whole facts file, but
   its own constant - the two are allowed to diverge later without
   touching each other). One explicit planned-row limit, `MAX_PLANNED_ROWS
   = 400`, used consistently by the schema (`maxProperties`), the importer,
   and this document - independent of the Done lane's `MAX_FACTS = 400`
   (same value today, a coincidence, not a shared constant).
2. **Empty rows.** `rows: {}` (see §1's reshaped schema) is an explicit,
   valid empty plan, subject to the same guarded-replacement rule as any
   other import (§3). `retire-plan` is still the only way to delete a
   plan's identity/revision outright; an empty plan keeps its id and
   revision until retired.
3. **Drift and CI.** CI gates strict JSON syntax, schema, and semantic
   validity (duplicate keys, uniqueness, bounds - everything in §1).
   `check-plan` (§2) stays advisory only; when it cannot compare (for
   example, no sidecar exists yet), it reports "unknown", never "in sync".
4. **Field vocabulary.** `phase`, `owner`, and `starts_when` stay bounded
   free text, not a closed vocabulary like `work_tags.STAGES` - a plan
   phase and a review/build stage mean different things. Whitespace-only
   values and control characters are rejected; ordinary Unicode text is
   accepted unchanged.
5. **Schema identification.** `schema_version` alone is sufficient for v1.
   A versioned schema artifact ships with an editor association; no second
   authoritative version field, no remote schema fetch during import. An
   unknown version refuses, unchanged from revision 1.
6. **CLI transition.** A clean break: `import-plan` accepts a JSON sidecar
   path only - no `.md` alias, no automatic sibling lookup, no heuristic
   fallback. PR #264 never shipped the Markdown-reading version, so there
   is nothing to stay compatible with. A wrong-extension or missing file
   gets an actionable error naming the expected sidecar path.

---

## 1. The format

### Recommended: Option A, a sidecar file

Next to a plan's Markdown file (for example `plan-board-lanes-v2-views.md`),
a sidecar file holds the same name with `.items.json` in place of `.md`:
`plan-board-lanes-v2-views.items.json`. `agenttalk board import-plan` reads
**only** this file (decision 6: a JSON path, nothing else accepted).

### The decoding contract (new in revision 2)

Before any schema or semantic check runs, the bytes must be decoded
**strictly**. Revision 1 described this only as "JSON parsing is exact" -
true of the JSON *grammar*, but not of Python's `json` module defaults,
which are more permissive than the grammar in ways that matter here (the
review's finding 1, citing
[Python's documented JSON interoperability notes](https://docs.python.org/3/library/json.html#standard-compliance-and-interoperability)):

1. Read the file as bytes, bounded by `MAX_SIDECAR_BYTES` (512 KiB) - the
   same read-one-byte-over-the-limit-and-refuse pattern `_read_plan_text`
   already used (`work_board_facts.py:709-722` on the parked branch).
2. Decode strictly as UTF-8 (`bytes.decode("utf-8")`, no `errors=` override).
   A leading byte-order mark decodes to U+FEFF, which is not valid JSON
   whitespace, so a BOM-prefixed file already fails to parse under this
   plan - no separate BOM-stripping step is proposed, and none is needed.
3. Before parsing, scan the decoded text's bracket nesting depth (`{`,
   `[` increment; `}`, `]` decrement, ignoring characters inside JSON
   strings). This sidecar's own schema never nests deeper than 4 (sidecar
   object → `rows` object → one row object → a string value); refuse,
   named "sidecar nesting too deep", at a depth above 8 - generous
   headroom, and small enough to bound the parser's own recursion before
   it ever runs, regardless of which JSON implementation parses it.
4. Parse with `json.loads`, with two non-default hooks:
   - `object_pairs_hook`: reject the object outright if any decoded key
     repeats at that nesting level - including the sidecar's own top
     level and every row. This directly closes finding 1's repro
     (`"plan_id":"alpha","plan_id":"victim"` - `json.loads` silently keeps
     the last one by default).
   - `parse_constant`: reject unconditionally. Python's `json.loads`
     recognizes the out-of-grammar tokens `NaN`, `Infinity`, and
     `-Infinity` by default and would otherwise hand back a float; none of
     this schema's fields are numeric in a way that should ever accept
     these.
5. After parsing, walk every string value in the document (recursively)
   and `encode("utf-8")` each one with strict error handling. An escaped,
   unpaired surrogate (for example `"\ud800"`) decodes into a valid Python
   `str` containing a lone surrogate code point, which is not valid UTF-8
   on its own; `str.encode("utf-8")` raises on it by default, which this
   step turns into a named refusal rather than a byte string that would
   fail again, confusingly, wherever it is next written or displayed.

Any failure at any of these five steps refuses the whole import, publishing
nothing - the same contract as a schema failure below.

### The schema (JSON Schema, draft 2020-12)

Revision 1's schema allowed two different rows to repeat the same
`work_item` (the review's finding 2: `maxItems`/`items` alone do not
express uniqueness, and `uniqueItems` only rejects two *byte-identical*
row objects). Revision 2 reshapes `rows` from an array of objects, each
carrying its own `work_item` field, into an **object map keyed by
`work_item`**. This is not a cosmetic change: a JSON object cannot contain
two members with the same key without failing the decoding contract's
duplicate-key check above - uniqueness and strict decoding become the
*same* mechanism, with one rule to maintain, not two that could drift
apart.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://agenttalk.dev/schemas/planned-lane/v1.json",
  "title": "agenttalk planned-lane sidecar",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "project_id", "plan_id", "plan_rev", "plan_name", "rows"],
  "properties": {
    "schema_version": { "type": "number", "enum": [1, 1.0] },
    "project_id": { "type": "string", "minLength": 1, "maxLength": 200 },
    "plan_id": { "type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,63}$" },
    "plan_rev": { "type": "string", "pattern": "^r[0-9]{1,30}$" },
    "plan_name": { "type": "string", "minLength": 1, "maxLength": 200 },
    "rows": {
      "type": "object",
      "maxProperties": 400,
      "additionalProperties": false,
      "patternProperties": {
        "^[a-z0-9][a-z0-9-]{0,63}$": {
          "type": "object",
          "additionalProperties": false,
          "required": ["phase", "owner", "starts_when"],
          "properties": {
            "phase": { "type": "string", "minLength": 1, "maxLength": 64 },
            "owner": { "type": "string", "minLength": 1, "maxLength": 200 },
            "starts_when": { "type": "string", "minLength": 1, "maxLength": 200 }
          }
        }
      }
    }
  }
}
```

Example, using the recommended `rows` shape (see §5 for the real plan):

```json
{
  "schema_version": 1,
  "project_id": "<store.project_id() for the target store>",
  "plan_id": "board-lanes-and-the-v2-team-views",
  "plan_rev": "r2",
  "plan_name": "board lanes and the v2 team views",
  "rows": {
    "wb-lanes-ui": {
      "phase": "1",
      "owner": "claude-agenttalk-frontend-dev (claude)",
      "starts_when": "wb-planned-lane merged"
    }
  }
}
```

Points the review raised, addressed directly:

- **`schema_version` numeric equality (finding 2).** JSON Schema's numeric
  types treat `1` and `1.0` as the same value, so `"enum": [1, 1.0]` above
  is really just `"enum": [1]` under the spec - written with both
  spellings so a human reading the schema sees that `1.0` is accepted, not
  only `1`. The hand-written Python check mirrors this exactly, rather
  than disagreeing with the schema in either direction:
  `isinstance(v, (int, float)) and not isinstance(v, bool) and v == 1`
  (the `not isinstance(v, bool)` guard matters because `bool` is a subtype
  of `int` in Python - `True == 1` - and this field must never accept
  `true`). A value passing this check is normalized to the Python `int`
  `1` before anything else reads it. Any other value, including a
  different number, a string, or a boolean, refuses as "schema_version
  unsupported" - the same named refusal as revision 1, just precisely
  specified now.
- **`plan_rev` has an explicit bound (finding 3).** `r` followed by 1 to
  30 digits (so `r999999999999999999999999999` is the longest accepted
  value, nowhere close to able to threaten the shared facts-file budget on
  its own). 30 digits is deliberately generous for a value that in
  practice is a small revision counter; the bound exists to make "a
  revision string of hundreds of thousands of digits" schema-invalid, not
  to constrain realistic use.
- **`rows` matches the 400-row limit (finding, confirmed with a
  qualification), using decision 1's `MAX_PLANNED_ROWS`.** The parked
  reader does not itself enforce `MAX_FACTS` on the planned section today
  - there is no existing 401-row read failure to point to - but reusing
  the *number* without a *consistent, independently-named* limit would
  have reproduced exactly the "schema says one thing, some reader enforces
  another" class this whole design exists to close. `maxProperties: 400`
  on `rows` is that one limit, named and used everywhere.
- **Pattern end-matching (finding, §1 patterns).** In several JSON Schema
  validator implementations - notably ones built on Python's `re` module,
  which several widely used JSON Schema libraries are - the `$` anchor
  matches immediately before a single trailing newline, not only at the
  true end of the string (a Python `re`-specific trait; the JSON Schema
  specification itself recommends ECMA-262 regex semantics, where `$`
  without a multiline flag has no such exception). A value like
  `"valid-id\n"` can therefore pass `pattern` validation under some
  validators while this design's own Python `fullmatch`-based importer
  correctly rejects it (`fullmatch` requires the entire string including
  the trailing `\n` to match, and no character class here includes `\n`).
  Two things follow, not one: validators SHOULD use an ECMA-262-conformant
  regex engine, as the specification itself already asks for; and,
  independent of any validator's behavior, **this document states
  explicitly, in prose, as a semantic rule separate from the regex: `plan_id`,
  `plan_rev`, and every `rows` key must not contain or end with a line
  terminator (`\n`, `\r`, ` `, ` `).** The importer's own
  `fullmatch` check is authoritative regardless of what any external
  validator concludes; the schema is published for interoperability, not
  as the importer's actual implementation.

### Accepts / refuses, exactly

The importer's contract is still binary: **the whole file is accepted
exactly as written, or the whole import refuses and nothing is written.**
There is no "skip this row, keep the rest" path. An empty `rows: {}` is
still a valid, accepted file (decision 2).

Accepted: a JSON object matching the schema above, after passing the
decoding contract, where (§3) the declared `project_id` matches the
target store and the guarded-replacement rule for an existing `plan_id`,
if any, is satisfied.

Refused, without writing anything, always with a named reason - the
decoding-contract failures above, plus:
- the file does not exist, is unreadable, exceeds `MAX_SIDECAR_BYTES`
  (512 KiB), or is not valid UTF-8;
- any unknown field, at the top level or inside any row;
- any missing required field, at the top level or inside any row;
- any type mismatch (a number, boolean, `null`, array, or object where a
  string is required; `rows` not an object; a row not an object);
- `schema_version` anything other than the normalized integer `1`;
- `project_id` not equal to the target store's own `store.project_id()`
  (§3);
- `plan_id`, `plan_rev`, or a `rows` key not matching its pattern, over its
  length cap, or containing/ending in a line terminator;
- `plan_name`/`phase`/`owner`/`starts_when` empty, whitespace-only,
  containing a disallowed control character, or over its length cap;
- more than 400 entries in `rows`;
- the guarded-replacement rule (§3) refusing a conflicting or stale
  revision against whatever is already stored for this `plan_id`.

### Why not Option B (a marked block inside the Markdown)

Unchanged from revision 1: a fenced block still depends on fence
recognition (which fence characters, which info string, zero vs. more
than one match, a block nested inside an unrelated example) - smaller
surface than the whole document, but still Markdown-shaped surface, which
PR #264's own history shows is not a safe place to draw the line.

### Why not Option C (a real CommonMark parser)

Unchanged from revision 1: this project ships no runtime dependencies
today (`dependencies = []` in `pyproject.toml`, confirmed unchanged on
`master`). A conformant parser would be the first one, and it still would
not decide "which table is the work-items table" on its own - exactly the
decision that produced eight review rounds' worth of findings. Not
recommended.

---

## 2. Lifecycle: missing input never deletes

**A sidecar's absence is not a statement about the plan.** Deleting or
renaming a previously imported sidecar file, or simply never running
`import-plan` again, leaves whatever is already stored for that `plan_id`
exactly as it was. The missing-file refusal happens before any write is
even attempted - there is no code path from "the file I was asked to read
is gone" to "therefore clear what was already recorded." **The only way to
remove a plan's rows from the board is `agenttalk board retire-plan
<plan_id>`,** an explicit, separate, named action - never an automatic
consequence of a missing or stale input file.

This makes the two files' authority explicit, which revision 1 only
implied: **the JSON sidecar is authoritative for board facts** (identity,
revision, and every row's phase/owner/starts-when). **The Markdown plan is
authoritative only for narrative** - why a plan exists, the decisions
behind it, context for a reader - and the importer never reads it, not
even to decide whether the sidecar file is still relevant. `check-plan`
(below) can only ever flag "these two disagree"; a clean `check-plan` run
is absence of a *detected* problem, never proof the two actually agree -
its own comparison is itself best-effort (§0 decision 3). A later edit to
a sidecar file, likewise, changes nothing on the board until
`import-plan` is run again and succeeds.

### `check-plan`, revised

```
agenttalk board check-plan <plan.md>
```

infers the sidecar path by convention (`<plan>.md` → `<plan>.items.json`)
and makes a best-effort, read-only, advisory comparison. Revision 1 only
compared row data; the review's finding on `check-plan` (comparing only
rows lets a revised Markdown plan keep an obsolete stored `plan_rev`/name
with a clean drift check) means **every duplicated top-level identity
field is compared too**: `plan_id`, `plan_rev`, and `plan_name`, in
addition to each row's `work_item`/`phase`/`owner`/`starts_when`. Per
decision 3, when `check-plan` cannot establish a comparison at all (no
sidecar file, an unreadable or unparseable one, or a Markdown table it
cannot confidently find with its own lenient scan), it reports
**"unknown"**, never "in sync" - silence about a problem it could not see
must never read as reassurance that there is none. `check-plan` still
never writes, and `import-plan` still never calls it.

---

## 3. Identity and guarded replacement

Making `plan_id` explicit (never derived from a title, as it was by
fallback in PR #264) removes *inference* ambiguity, but the review is
right that it does not by itself prevent *collision*: two independently
written sidecars can both declare `plan_id: "alpha"`, and PR #264's
"different plan" title-mismatch refusal is gone now that there is no
title to compare. Two further rules close this, both evaluated inside
`_write_section`'s existing lock (so they are atomic with respect to any
other writer of the same facts file, including a concurrent `retire-plan`
or another `import-plan`):

**Project binding.** The sidecar's required `project_id` field must equal
`store.project_id()` for the store the command is actually run against.
A mismatch refuses, named "sidecar project_id does not match this store" -
closing the review's repro of running `import-plan` in project B against
a path that happens to hold project A's otherwise-valid sidecar.

**Guarded replacement, keyed by `plan_rev`.** `plan_id` is a stable
identity, assigned once by the lead and never re-derived; `plan_rev` is
both that plan's own revision label *and* the anchor this design uses to
decide whether an import may replace what is already stored. Inside the
locked build callback, comparing the incoming sidecar against whatever is
currently stored for this `plan_id` (if anything):

- **no existing entry** - the import is a fresh registration; always
  accepted.
- **an existing entry, byte-identical incoming content** (same
  `plan_rev`, `plan_name`, and `rows`) - idempotent: the import succeeds,
  nothing actually changes, and re-running the same `import-plan` command
  twice is always safe.
- **an existing entry, same `plan_rev`, different content** - refused,
  named "conflicting content under the same revision" - content changed
  without the revision label changing, which this design treats as a
  confused-identity signal to surface, not silently resolve.
- **an existing entry, incoming `plan_rev`'s number is lower than or
  equal to (but not identical to) the stored one** - refused, named
  "stale revision" - the ordinary `import-plan` path never rolls a plan
  backward. A deliberate rollback is possible but must be its own loud,
  separate action (a proposed `--allow-revision-rollback` flag on
  `import-plan`, logged distinctly), never a side effect of running the
  plain command against an old file.
- **an existing entry, incoming `plan_rev`'s number is strictly higher,
  content differs** - the ordinary, expected case: the import replaces
  the stored entry.

This is "default import does not silently replace a different document"
in concrete terms: the only two outcomes for an existing `plan_id` are
"this is recognizably the same plan, moving forward" (replace) or "this
doesn't look like a safe forward move" (refuse, by name). There is no
third outcome where an import just overwrites whatever was there.

---

## 4. What this design reuses, what it must port, and what it deletes

**Correction from revision 1:** revision 1's reuse table stated that
`planned_cards` and the console's `PLANNED` label already existed on
`master`. They do not - those, and everything else this section lists
under "must be ported," exist only on the parked `feat/wb-planned-lane`
branch (head `d3cc6557731a16de295482426681c56db667aefd`), which never
merged. The follow-up build PR is not "swap the parser inside existing
code" - it is "port the planned-lane machinery from the parked branch,
with the fixes in §1-§3 built in, onto current `master`." This section is
organized by what is actually true today, checked by diffing the parked
branch against `origin/master` directly.

### Already on `master` today - genuinely reusable as-is

| Piece | Where |
|---|---|
| `MAX_FILE_BYTES = 512 * 1024`, the file-size bound pattern | `work_board_facts.py:23` |
| `_SLUG` and the other shared regexes | `work_board_facts.py:29-31` |
| `_read` - the bounded, schema-checked facts-file reader | `work_board_facts.py:272-289` |
| `_write_section`'s **basic** shape - one facts lock, a schema/oversize refusal, a `session_id`-in-the-passed-section re-check before writing | `work_board_facts.py:292-310` |
| `work_tags.value("work_item", ...)` - the canonical work-item slug validator, `fullmatch`-based | `work_tags.py:48-73` |
| `store.project_id()` - the path-derived project identifier §3 binds against | `store.py:3328-3336` |
| The whole Done lane (`verify_merges`, `load_integration`, `_valid_fact`, etc.) | untouched either way; does not import plans |
| `dependencies = []` | `pyproject.toml:13`, confirmed unchanged |

### Exists only on the parked `feat/wb-planned-lane` branch - must be ported, not merely reused

| Piece | What it does | Porting note |
|---|---|---|
| `_write_section`'s **merge-capable** extension (`build=` callback, `_UNSET` sentinel, a session re-check timed immediately before the atomic write rather than wherever the caller happened to capture it) | Lets `import-plan`/`retire-plan` each touch only their own plan id's entry inside the shared "planned" section without clobbering another plan or a concurrent writer | Port, then apply §3's guarded-replacement comparison *inside* this same build callback - not a second, separate critical section |
| `_valid_planned_row`, `_valid_planned_entry`, `_valid_planned_section` | The stored shape's own exact-match validators (fail-closed before any write, recast principle B) | Port unchanged in spirit; `_valid_planned_row`'s key set changes to match the new sidecar-derived row shape (`phase`/`owner`/`starts_when`; `work_item` is now the map key, not a sibling field, at the STORED level too, for the same uniqueness-by-construction reason as §1) |
| `_planned_build` | The shared `_write_section` build-callback factory for import-plan/retire-plan | Port; the "different plan" title-mismatch branch it used to guard is deleted (no title to compare); §3's rules replace it |
| `retire_plan` | Removes one plan id's rows; the only deletion path (§2) | Port unchanged |
| `load_planned` / `_load_planned` | The server-side, git-free reader the board's feed calls; bounded, capped warnings | Port unchanged; still a separate, open item that this design does not touch (§5, #265 item 2's general half) |
| `planned_cards` | The dispatched-vs-planned reducer | Port unchanged |
| `work_board_feed.py`'s call to `planned_cards` (parked branch only, line ~161; **no such call exists on `master`**) | Wires planned rows into the board's feed | Port this call site into `master`'s current `work_board_feed.py` |
| `console2.js`'s `planned: 'PLANNED'` label (parked branch only, line 937; **no such entry exists on `master`**) | The console's column label | Port this one label-map entry |
| `cli.py`'s `cmd_board` `import-plan`/`retire-plan` actions and their argparse subparsers | The CLI surface | Port, with `import-plan`'s argument changed from a `.md` path to a `.json` sidecar path (decision 6) and the new project/replacement checks wired in |

The build PR must also **verify Done-lane parity is unaffected** by
porting this (the parked branch's own review history already did this
once; it needs re-confirming against current `master`'s Done-lane code,
which may have moved since the branch was parked).

### Deleted: every Markdown parser

Unchanged from revision 1 - these exist only on the parked branch and are
not ported at all, by name, in
`src/agenttalk/work_board_facts.py`: `_Line`, `_classify_line`,
`_classify_lines`, `_HEADING_RE`, `_TITLE_TEXT_RE`, `_FENCE_RE`,
`_FIELD_RE`, `_REVISION_VALUE_RE`, `_SECTION5_TEXT_RE`, `_SEP_CELL`,
`plan_id_of`, `_split_row`, `_plan_table`, `_parse_plan`,
`_read_plan_text` (replaced by the §1 decoding-contract reader, which
reuses its size/UTF-8-bound *pattern*, not its body).

---

## 5. Which #265 items this design affects, and one new item it does not close

| # | Gap | Status |
|---|---|---|
| 1 | A reset landing between the fresh-session check and the write can still let old data reappear. | **Stays open.** Lives in `_write_section`'s check-then-write ordering (§4: the merge-capable extension must still be ported and still has this property). The review's added guidance: prefer closing this with coordinated locking (one atomic check-and-write step) over a post-write rollback, since a blind rollback could erase a legitimate, newer concurrent writer's work - noted here for whoever implements the fix. |
| 2 | One huge value can still produce one oversized warning/message. | **Partly addressed going forward, for new imports.** Every sidecar field now has an explicit length cap enforced before any write (§1), and the only message that used to embed an unbounded value verbatim (the title-collision refusal) no longer exists (§3 replaces it). `_load_planned`'s own warning-byte bound, for a *stored* document an older importer or a hand-edit produced, is unchanged and stays open. |
| 3 | "Exact shape" checking is not exact: an unknown top-level key in the stored section is silently dropped on the next write; the stored `session_id` is never shape-checked. | **Stays open.** `_valid_planned_section` is ported with the same scope as before (§4); this design does not touch its top-level key-set or `session_id` validation. |
| 4 | The board does not show the planned details in the UI. | **Stays open**, unaffected either way - `wb-lanes-ui` scope. |

**A fifth gap, found during this design's own review, not one of the
original four #265 items:** the ported `_write_section` refuses on an
unsupported schema or an oversized file, but when `_read()` returns a
*malformed or unreadable* result (not a genuinely *missing* file), the
existing code (`doc = doc or {"schema_version": SCHEMA_VERSION}`) treats
that the same as "missing" and silently proceeds with a **fresh, empty
document** - discarding whatever was in the damaged file, including
unrelated sections (the Done lane's facts, another plan's rows). This is
not something §1-§3 touch; it lives entirely in `_write_section` itself,
reused by every writer of this facts file. Because a correctly-formed new
sidecar could trigger exactly this path against an already-damaged file,
**this is treated as load-bearing for the build PR, not a deferred
follow-up**: the build must distinguish "genuinely missing" (safe to
initialize fresh, as today) from "present but malformed/unreadable" (must
refuse the write outright, leaving the damaged file for the operator to
investigate, the same way an unsupported schema or an oversized file
already refuse rather than silently resetting).

---

## 6. Acceptance test

Revision 1's acceptance test exercised only one successful import against
a changing live-store copy. The review is right that this does not bound
the behavior that matters most here (all-or-nothing refusal) and is not
reproducible. Revision 2 replaces it with a **pinned fixture** and an
explicit set of negative cases.

### The fixture

A disposable store copy, never the live one, seeded with a **fixed**
dispatch history giving exactly two of the real plan's seven work items a
dispatch: `wb-done-facts` and `wb-planned-lane` (matching this project's
own actual history, but hard-coded into the test fixture, not read from
the live bus at test time - the assertion below must not depend on
whatever is dispatched in reality when the test happens to run).

### Positive case

1. Hand-write `plan-board-lanes-v2-views.items.json` from
   `atk-scratch/lead/plan-board-lanes-v2-views.md`'s own section 5:
   `schema_version: 1`, `project_id` equal to the fixture store's own
   `project_id()`, `plan_id: "board-lanes-and-the-v2-team-views"`,
   `plan_rev: "r2"`, `plan_name: "board lanes and the v2 team views"`, and
   the 7 rows keyed by `work_item`.
2. `agenttalk board import-plan plan-board-lanes-v2-views.items.json`
   against the fixture store. Must succeed (exit 0), all 7 rows recorded.
3. Load the board's feed against the same store. `wb-done-facts` and
   `wb-planned-lane` must show their own derived column, not Planned
   (dispatched always wins - asserted as its own, separate check from the
   next one). The other 5 rows must show as Planned, carrying their
   phase/owner/starts-when from the sidecar.
4. Re-run the identical `import-plan` command a second time. Must succeed
   (exit 0), idempotent: the stored facts are byte-identical before and
   after this second run (§3).

### Negative cases - each one asserts a non-zero exit AND that the stored
### facts file is byte-identical before and after, including an unrelated
### second plan's entry and the Done-lane section

- a duplicate top-level key (`"plan_id":"a","plan_id":"b"`, or the
  escaped-equivalent form the decoding contract also covers);
- a duplicate `rows` key arriving via raw duplicate JSON object keys
  (confirming the review's uniqueness finding is closed by the decoding
  contract, not a separate check that could itself have a gap);
- malformed JSON (truncated, a trailing comma, unbalanced brackets);
- a file over `MAX_SIDECAR_BYTES`;
- an unsupported `schema_version` (a string, `2`, `true`, `1.1`) and,
  separately, a positive confirmation that `1.0` (the float) IS accepted
  and normalizes to the same stored result as `1` (the int);
- an unknown top-level field, and separately an unknown field inside one
  row;
- invalid UTF-8 bytes; a `"\ud800"` lone-surrogate escape; JSON nesting
  past the bound;
- `NaN`/`Infinity`/`-Infinity` in place of any field;
- a `project_id` that does not match the target store;
- a conflicting replacement: same `plan_id`, same `plan_rev` as what's
  already stored, different content; and separately, a numerically lower
  `plan_rev` than what's stored (without the rollback flag);
- explicit `rows: {}` (empty plan) - a **positive** case, asserted
  separately: accepted, stored with zero rows, previously-Planned cards
  from this plan_id disappear from the feed;
- corrupt pre-existing state: seed the fixture store's facts file with
  already-malformed bytes (not genuinely missing) *before* attempting an
  otherwise-valid import; the import must refuse (§5's new fifth gap),
  leaving the malformed file exactly as it was, not replaced with a fresh
  document seeded from the new sidecar.

No source changes are proposed in this PR; the above is the acceptance
bar for the follow-up build PR to implement against, not evidence that
anything here currently runs.

---

## 7. Open questions for the reviewer

Revision 1's six open questions are now decisions (§0). Remaining,
narrower questions raised by this revision's own new mechanisms:

1. **Is `--allow-revision-rollback` (§3) the right shape for a deliberate
   rollback** - a flag on the ordinary `import-plan` command - or should
   rollback be its own subcommand, closer to how `retire-plan` is already
   separate from `import-plan`, so it cannot be reached by a typo on a
   routine import?
2. **Is a nesting-depth bound of 8 (§1) too generous, too tight, or about
   right**, given the schema's own real depth is 4? It is chosen only to
   bound the parser before it runs, not to express anything about valid
   shapes.
3. **Should the new fifth gap (§5: malformed-vs-missing in
   `_write_section`) be filed as its own tracked issue now, or stay
   documented only here until the build PR fixes it as a prerequisite?**
   This design treats it as load-bearing for the build, not deferred, but
   does not itself open a tracking issue.
4. **For `check-plan`'s expanded comparison (§2): when the Markdown's own
   lenient table scan genuinely cannot find a comparable value (not an
   error, just an ordinary plan written before this convention existed),
   should that field be reported "unknown" individually, or should the
   whole comparison for that plan report "unknown"?** The document
   currently implies the latter (any inability to compare means
   "unknown", not a partial comparison) but does not say so explicitly.

## Technical details

- Supersedes the Markdown-reading approach in PR #264 (branch
  `feat/wb-planned-lane`, parked at
  `d3cc6557731a16de295482426681c56db667aefd`).
- Driven by issue #266, issue #265 (§5), and this design's first review
  (codex-agenttalk-developer-4, scratch task `tk-bde9e66e8f1a`, 11 inline
  comments on revision 1 at `27b872e661fb082a2f6695b9f97fc51ba07de43c`,
  10 of 11 confirmed; the design SHA/`master`-base reuse-inventory
  correction and the dangling `_valid_sidecar` pointer - this revision's
  §4 uses the name `_load_sidecar` consistently instead).
- Revision 1's eight-round review trail on PR #264 is unchanged from
  before: tk-c7e4f7e5fb2f, tk-53a01ba76dc5, tk-1ee3b4eee094,
  tk-96d922e450fe, tk-ced6fee80731, tk-2551454377c0, tk-91f0d33a23ea,
  tk-3b138204d171 (all codex-agenttalk-developer-4).
- §4's reuse/port inventory is drawn from a direct `git diff
  origin/master...feat/wb-planned-lane` on
  `src/agenttalk/work_board_facts.py`, `work_board_feed.py`, `cli.py`, and
  `web_static/console2.js`, confirming line-for-line which functions and
  call sites exist on `master` today versus only on the parked branch.
