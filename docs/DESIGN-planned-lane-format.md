# A strict plan sidecar for the Planned lane

Status: proposed. Date: 2026-10-02.

## In plain words

This is a proposal, not something you can run yet. It is about how the work
board learns which work is *planned* but not started.

Today, planned work lives only inside a plan's Markdown file - the same
file a person reads. Teaching a program to find the work-items table inside
that free-form prose turned out to be unsafe: across eight review rounds
(PR #264), every fix closed one way to hide or fake a row, and every round
a reviewer found a new one - a heading typed without a space, a plan id
tucked into an HTML comment invisible on the rendered page, an indented
example table that silently replaced the real one. The reviewer's own
verdict: free-form Markdown, used as a data contract, has no bottom to this
list.

This document proposes to stop trying. Plans stay written in Markdown, for
people to read. But the few facts the board needs - which work items, in
what order, for whom - move into a second, small, strict file next to it:
a sidecar in JSON, checked against a published schema before anything is
read from it. JSON parsing has no heading styles, no code fences, no
comment syntax to hide inside. There is exactly one way to write a valid
row, and every other way refuses, without writing anything.

What you will notice, once this is built: nothing changes in how a plan
reads as a document. A new file sits next to it (`<plan>.items.json`), and
`agenttalk board import-plan` reads that file instead of the Markdown.

What you need to do, right now: nothing - review this design and raise the
open questions at the end. No code changes are proposed yet.

---

## 1. The format

### Recommended: Option A, a sidecar file

Next to a plan's Markdown file (for example `plan-board-lanes-v2-views.md`),
a sidecar file holds the same name with `.items.json` in place of `.md`:
`plan-board-lanes-v2-views.items.json`. `agenttalk board import-plan` reads
**only** this file. It never opens, globs for, or infers anything from the
Markdown file at all - the two files are not linked by the importer in any
way (see §2 for how they stay in step by a *different*, advisory means).

This is a deliberate narrowing of today's CLI contract: PR #264's
`import-plan <file>` took the plan's `.md` path. Under this proposal
`import-plan <file>` takes the sidecar's `.json` path instead. Because PR
#264 never merged, nothing has shipped with the old contract - there is
nothing to migrate.

#### The schema (JSON Schema, draft 2020-12)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://agenttalk.dev/schemas/planned-lane/v1.json",
  "title": "agenttalk planned-lane sidecar",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "plan_id", "plan_rev", "plan_name", "rows"],
  "properties": {
    "schema_version": { "const": 1 },
    "plan_id": { "type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,63}$" },
    "plan_rev": { "type": "string", "pattern": "^r[0-9]+$" },
    "plan_name": { "type": "string", "minLength": 1, "maxLength": 200 },
    "rows": {
      "type": "array",
      "maxItems": 500,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["work_item", "phase", "owner", "starts_when"],
        "properties": {
          "work_item": {
            "type": "string",
            "pattern": "^[a-z0-9][a-z0-9-]{0,63}$"
          },
          "phase": { "type": "string", "minLength": 1, "maxLength": 64 },
          "owner": { "type": "string", "minLength": 1, "maxLength": 200 },
          "starts_when": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          }
        }
      }
    }
  }
}
```

This document is the published contract. The importer is not required to
run a general-purpose JSON Schema validator against it (agenttalk ships no
runtime dependencies today, and a hand-written check mirroring this schema
is small - see §3's `_valid_sidecar` for the proposed function); the
schema's job is to be the one place that says what "valid" means, in a form
a person or another tool can check independently of agenttalk's own code.

#### Accepts / refuses, exactly

The importer's contract is binary: **the whole file is accepted exactly as
written, or the whole import refuses and nothing is written.** There is no
"skip this row, keep the rest" path at all - every accepted row is accepted
whole, every rejected file is rejected whole. This is the single biggest
change from the Markdown importer, which skipped individual malformed rows
while continuing to import the rest (the exact mechanism several of the
eight rounds' findings hid behind). An empty `rows: []` is still a valid,
accepted file - clearing a plan's rows is an intentional, explicit act (an
empty array), never confused with "every row happened to be unreadable."

Accepted:
- a JSON object (not an array, string, or other top-level value) with
  **exactly** the five keys above - no more, no fewer;
- `schema_version` exactly the integer `1` (the one version this importer
  understands; see "schema_version unsupported" below for any other value);
- `plan_id` a lowercase slug, 1-64 characters (`[a-z0-9][a-z0-9-]*`) -
  **always explicit, in every sidecar.** Unlike PR #264, there is no
  fallback derivation from a title. This alone deletes the entire class of
  "plan id derived from a title, collides with a different plan's id"
  bugs two of the eight rounds found (an appendix-ID hijack, a commented-ID
  hijack) - there is no title for the importer to read in the first place;
- `plan_rev` matching `r` followed by one or more digits (`r1`, `r2`, ...);
- `plan_name` a non-empty string, capped at 200 characters;
- `rows` an array of 0 to 500 objects, each with **exactly** the four keys
  `work_item`, `phase`, `owner`, `starts_when` - no more, no fewer;
- `work_item` validated by the SAME function the rest of agenttalk already
  uses for this exact field, `work_tags.value("work_item", ...)`
  (`src/agenttalk/work_tags.py:48`) - a lowercase slug, 1-64 characters -
  reused unchanged, not reimplemented;
- `phase`, `owner`, `starts_when`: non-empty strings, each capped at a
  bounded length (64 for `phase`, 200 for `owner` and `starts_when` -
  matching the existing columns' own rough shape; these are deliberately
  generous, not tight, since they hold free text like "owner (vendor)"
  strings or a prose starts-when condition).

Refused, without writing anything, always with a named reason:
- the file does not exist, is unreadable, exceeds the existing
  `MAX_FILE_BYTES` bound (512 KiB, `work_board_facts.py:31`), or is not
  valid UTF-8 - the same three checks `_read_plan_text` already makes
  (`work_board_facts.py:709-722`), reused for the sidecar's bytes instead
  of the Markdown's;
- the bytes do not parse as JSON, or the parsed value is not a JSON object;
- **any unknown field**, at the top level or inside any row (for example a
  stray `"notes"` key) - refused, never silently accepted and dropped;
- **any missing required field**, at the top level or inside any row;
- **any type mismatch** - a number, boolean, `null`, array, or object where
  a string is required; `rows` not being an array; a row not being an
  object;
- `schema_version` anything other than the integer `1` - named explicitly
  as "schema_version unsupported" (an older or newer importer talking past
  this one), never silently upgraded, downgraded, or ignored;
- `plan_id` or `plan_rev` or `work_item` not matching its pattern, or
  `plan_name`/`phase`/`owner`/`starts_when` empty or over its length cap;
- `rows` longer than 500 items;
- **a duplicate `work_item`** anywhere in `rows` - the whole import
  refuses (this already matched PR #264's behavior for a duplicate found
  inside one table; it is unchanged here, now simply true for every
  duplicate, with no per-row skip to hide behind).

### Why not Option B (a marked block inside the Markdown)

A fenced block with an info string like `agenttalk-plan`, holding the same
JSON, keeps everything in one file. But the importer still has to find
"the" block: it must recognize fence syntax (`` ``` `` vs `~~~`, three vs
more characters, an info string with or without trailing text), decide
what happens with zero or more than one such block, and decide whether a
block inside a *second*, nested fence (a Markdown example showing someone
else's fenced plan) counts. That is a smaller surface than parsing the
whole document, but it is still Markdown-shaped surface, and PR #264's own
history is that "a smaller surface" was tried more than once (the CUT
round deleted general fence parsing down to exactly this kind of
single-purpose recognition, for the session-capture marker) and still left
room for a near-miss. A sidecar file has no fence to recognize in the
first place - the importer opens a path and calls `json.loads` on it.

### Why not Option C (a real CommonMark parser)

This project ships no runtime dependencies today
(`dependencies = []` in `pyproject.toml`). A conformant CommonMark parser
would be the first one, and a meaningful one to vet and pin. It would also
not fully solve the problem it is brought in for: CommonMark defines how a
renderer turns Markdown into HTML, not which of several rendered tables is
"the" work-items table, or how a heading's accepted forms should map to a
plan's section boundaries - exactly the decisions that produced eight
rounds of findings under a hand-written parser. A real parser raises the
floor on which near-misses are *possible*, but the decision of "which
table, which heading" is still agenttalk's own code to get right. It is
not recommended.

---

## 2. Who writes the sidecar, and staying in step

**The lead writes the sidecar by hand, as part of writing or revising the
plan**, the same way the lead writes the Markdown today. There is no
proposed tool that *generates* the sidecar from the Markdown - building one
would mean building a Markdown reader again, the exact thing this design
removes.

To catch the sidecar and the Markdown drifting apart (a row added to
section 5 and forgotten in the sidecar, or the reverse), a new read-only,
advisory command is proposed:

```
agenttalk board check-plan <plan.md>
```

`check-plan` infers the sidecar path by convention (`<plan>.md` to
`<plan>.items.json`) and makes a **best-effort**, read-only comparison: it
does its own simple, lenient scan of section 5's table (reusing exactly
the loose table-finding PR #264 already had, with no attempt at
exactness) and reports, in plain text to the lead's own terminal:

- a `work_item` present in the Markdown table but missing from the
  sidecar's `rows`;
- a `work_item` present in the sidecar but missing from the Markdown
  table;
- a `phase`/`owner`/`starts_when` that differs between the two for the
  same `work_item`;
- a missing sidecar file at all (exit code non-zero, so a forgotten sidecar
  is visible in CI or a pre-dispatch check, not just on the lead's own
  screen).

`check-plan` **never writes anything and is never called by
`import-plan`.** It cannot block an import, and a bug in its own lenient
scan cannot corrupt the board - the worst it can do is miss a drift it
should have reported, or report one that is not really there. That is
exactly why its own Markdown-reading code is allowed to be loose: it is
advisory, not a trust boundary. **The board trusts only the sidecar.**
`import-plan` never consults `check-plan`, and a plan with no sidecar at
all simply has no planned rows on the board - there is no fallback to the
Markdown.

---

## 3. Reused unchanged vs. deleted

Everything below is in `src/agenttalk/work_board_facts.py` unless noted.

### Reused unchanged

| Piece | What it is |
|---|---|
| `_write_section` | The one facts-file writer every caller (verify-merges, import-plan, retire-plan) shares: the atomic replace, the session-id re-check immediately before the write (the "session fence"), the schema/size refusals. |
| `_read` | The bounded, schema-checked facts-file reader (512 KiB bound, `schema_version` check). |
| `MAX_FILE_BYTES`, `MAX_FACTS` | The existing size bounds, reused for the sidecar file too. |
| `_valid_planned_row` | The stored row's exact-shape check (`set(row) == _PLANNED_ROW_KEYS`, with `work_item` slug-checked and the rest non-empty strings). Unchanged: it validates the *stored* shape, which is unchanged by this proposal. |
| `_valid_planned_entry` | The stored per-plan entry's exact-shape check (`project`, `plan_rev`, `plan_name`, `written_at`, `rows`). |
| `_valid_planned_section` | The fail-closed check of the WHOLE stored "planned" section before any write (recast principle B) - refuses a write rather than silently discarding an unexpected existing shape. |
| `_planned_build` | The shared `_write_section` build-callback factory for import-plan and retire-plan: validates the existing section, unwraps "plans", leaves every other plan id's entry untouched. |
| `retire_plan` | Unchanged in full: removes one plan id's rows, refuses if none exist. Still keyed by `plan_id` alone, which every sidecar now always carries explicitly. |
| `load_planned` / `_load_planned` | The server-side, git-free reader the board's feed calls: bounded, capped warnings (`_MAX_PLANNED_WARNINGS`), per-project filtering. |
| `planned_cards` | The dispatched-vs-planned reducer: a work_item with a dispatch keeps its derived column; a work_item claimed by two plan ids is "unknown"; the rest become Planned cards. |
| `work_board_feed.py` line ~161 | Calls `work_board_facts.planned_cards(dispatched, planned)` only - it never touches a plan file, Markdown or JSON, directly. Nothing here changes. |
| `src/agenttalk/web_static/console2.js:937` | The `planned: 'PLANNED'` label map entry. A card's column name is unchanged by this proposal. |
| The Done lane | `verify-merges`, `load_integration`, and every Done-lane function are untouched; they do not import plans at all. |
| `_SLUG`, `_OID`, `_REF` and the other shared regexes | Reused for `plan_id` (`_SLUG`) and anywhere else they already applied. |
| `work_tags.value("work_item", ...)` | Reused unchanged as `work_item`'s validator (`src/agenttalk/work_tags.py:48`). |

### Deleted: every Markdown parser

| Function / constant | What it did, and why it goes |
|---|---|
| `_Line` (dataclass), `_classify_line`, `_classify_lines` | The one-line-classifier recast's core: turned each raw line into a typed, validated token (title/heading/fence/table_row/field/other). No longer needed - the sidecar has no lines to classify, only JSON values to type-check. |
| `_HEADING_RE`, `_TITLE_TEXT_RE`, `_FENCE_RE`, `_FIELD_RE`, `_REVISION_VALUE_RE`, `_SECTION5_TEXT_RE`, `_SEP_CELL` | Every regex that recognized a piece of Markdown syntax (an ATX heading, a title line, a fence, a `Plan id:`/`Plan revision:` field, a table separator row). None of these concepts exist in JSON. |
| `plan_id_of` | Derived a plan id from a title by slugifying it. Deleted outright, not just unused: the sidecar's `plan_id` is always explicit, so there is no title to derive one from, and no collision class to guard against. |
| `_split_row` | Split one Markdown table row on unescaped `|`. JSON has no table rows. |
| `_plan_table` | Found "the" table under the one `## 5. Work items` heading, enforcing no-fence-before-it and no-second-table-after-it. JSON has no headings or tables to disambiguate. |
| `_parse_plan` | The top-level plan parser: found the title/id/revision in the header block, called `_plan_table`, mapped header-cell names to columns, built rows, decided which rows to skip vs. which problems are fatal. Replaced by a new, much smaller `_load_sidecar` that calls `json.loads` and checks the shape above - no header/body distinction, no per-row skip, no column-name matching. |
| `_read_plan_text` | Read the Markdown file's bytes with a size/UTF-8 bound. Replaced by an equivalent (same bound, same UTF-8 discipline) bytes reader for the sidecar's JSON bytes - the *pattern* is reused, the function is renamed and no longer assumes Markdown. |

`import_plan`'s own body changes to call the new sidecar reader instead of
`_read_plan_text` + `_parse_plan`; its surrounding contract (capture
`project`/`session_id` once before any read, build the new per-plan entry,
call `_write_section` with the existing `_planned_build` check) is
unchanged line-for-line.

---

## 4. Which #265 items this closes

| # | Gap | Status under this design |
|---|---|---|
| 1 | A reset landing between the fresh-session check and the write can still let old data reappear. | **Stays open.** This lives entirely in `_write_section`'s own check-then-write ordering, which is reused unchanged. Still needs its own fix (one atomic step, or a re-check-and-rollback after the write) regardless of input format. |
| 2 | One huge broken value produces one enormous warning that still crowds out a real card, even with the warning-count cap. | **Partly closed, going forward.** Every sidecar field now has an explicit `maxLength` (200 for `plan_name`/`owner`/`starts_when`, 64 for `phase`/`work_item`/`plan_id`) checked *before* anything is written, so the new importer can never write an oversized value that could later produce an oversized warning or message - and the only message that ever embedded a plan name verbatim (`import_plan`'s "already recorded for a different plan" refusal) no longer exists, because plan id is no longer derived from a title at all. The *general* hardening #265 asked for - bounding every warning's own byte length in `_load_planned`, for a stored document a different, older importer or a hand-edit produced - is unchanged and stays open. |
| 3 | "Exact shape" checking is not exact: an unknown field in the stored section's top level is accepted and silently dropped by the next write; the stored `session_id` is never shape-checked. | **Stays open.** `_valid_planned_section` is reused unchanged; it still checks only `schema_version` and `plans`, not the section's own exact key set or `session_id`'s shape. Not touched by this proposal (a sidecar-format change does not change the *stored* section's shape). |
| 4 | The board shows PLANNED but not the phase/owner/starts-when. | **Stays open**, unaffected either way - this is `wb-lanes-ui`'s console-side scope, which already reads `planned_cards`' existing output; the sidecar changes nothing about what that function returns. |

---

## 5. Acceptance test

The operator's real plan, `atk-scratch/lead/plan-board-lanes-v2-views.md`,
converted to a sidecar, on a **copy of the store** (never the live one):

1. Hand-write `plan-board-lanes-v2-views.items.json` from the plan's own
   section 5 table: `plan_id = "board-lanes-and-the-v2-team-views"`,
   `plan_rev = "r2"`, `plan_name = "board lanes and the v2 team views"`,
   and the 7 rows (`wb-done-facts`, `wb-planned-lane`, `wb-lanes-ui`,
   `v2-team`, `v2-history`, `v2-learning`, `v2-walkthrough`), each with its
   phase, owner and starts-when exactly as section 5 lists them.
2. On a disposable copy of the store, run
   `agenttalk board import-plan plan-board-lanes-v2-views.items.json`.
   It must succeed (exit 0) and report all 7 rows recorded, 0 skipped (the
   new contract has nothing left to skip - a row is recorded or the whole
   import refuses).
3. Load the board's feed (`work_board_feed.build`) against that same store
   copy. `wb-done-facts` and `wb-planned-lane` already have real dispatches
   in this project's own bus history, so `planned_cards` must place them in
   their own derived column, not Planned (dispatched always wins).
   The other 5 rows - `wb-lanes-ui`, `v2-team`, `v2-history`,
   `v2-learning`, `v2-walkthrough` - have no dispatch yet, so they must
   appear as Planned cards, carrying their phase/owner/starts-when from the
   sidecar.
4. This is the acceptance bar for the follow-up implementation PR, not
   something this docs-only PR runs - no source changes are proposed here.

---

## 6. Open questions for the reviewer

1. **Is the sidecar's own file bound (512 KiB, reusing `MAX_FILE_BYTES`)
   right for JSON, or should it be smaller?** A 500-row plan is nowhere
   near 512 KiB even at the generous per-field caps above; is the shared
   constant the right one to reuse, or should the sidecar have its own,
   tighter bound to make the two uses independently tunable later?
2. **Is `rows: []` (an explicit, intentional "clear every row") the right
   way to represent clearing a plan, or should clearing be a separate,
   named action (closer to `retire_plan`) so an empty array can never be
   mistaken for a mistake?**
3. **Should `check-plan`'s drift report be wired into CI** (so a plan
   merged with a stale sidecar fails a check), or stay a lead-run,
   terminal-only command for now, as proposed?
4. **Is per-field length (`maxLength`) the right bound for `phase`,
   `owner`, and `starts_when`, or should these instead be drawn from a
   smaller, closed vocabulary** (like `work_tags.STAGES` already is for
   `stage`) now that the format is strict enough to enforce one?
5. **Should the sidecar carry its own `$schema` URL as a field** (so a
   tool reading the file can self-identify which schema to validate
   against), or is a bare `schema_version` integer enough, matching how
   the stored facts file itself is versioned today?
6. **Does `import-plan` taking a `.json` path instead of a `.md` path need
   a transition alias** (accept either extension, inferring intent from
   the extension) for operator muscle memory, or is a clean break fine
   since PR #264 never shipped the old contract?

## Technical details

- Supersedes the Markdown-reading approach in PR #264 (branch
  `feat/wb-planned-lane`, parked at
  `d3cc6557731a16de295482426681c56db667aefd`).
- Driven by issue #266 (the parking decision and the three options) and
  issue #265 (the four follow-up gaps evaluated in §4).
- The eight review rounds and their findings: tk-c7e4f7e5fb2f,
  tk-53a01ba76dc5, tk-1ee3b4eee094, tk-96d922e450fe, tk-ced6fee80731,
  tk-2551454377c0, tk-91f0d33a23ea, tk-3b138204d171 (all
  codex-agenttalk-developer-4). The final two, which parked the PR:
  tk-91f0d33a23ea found the no-space-heading/header-boundary bug;
  tk-3b138204d171 found that the classifier still treated an
  HTML-commented field as live metadata and an indented example table as
  a real one.
- Reused/deleted inventory in §3 is drawn directly from
  `src/agenttalk/work_board_facts.py` on `feat/wb-planned-lane` at the
  parked head, and from `src/agenttalk/work_board_feed.py` and
  `src/agenttalk/web_static/console2.js` on `master`.
