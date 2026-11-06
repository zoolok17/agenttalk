# A strict plan sidecar for the Planned lane

Status: proposed, revision 3 (RECAST - read at display time, no import).
Date: 2026-10-02.

## In plain words

This is a proposal, not something you can run yet. It is about how the work
board learns which work is *planned* but not started.

Revisions 1 and 2 proposed a JSON sidecar file that an `import-plan`
command would read once and copy into the board's shared facts file,
replacing an earlier attempt to read this straight out of a plan's
Markdown (which could not be made safe - see the "Background" note below).
A second design review found that almost every remaining finding, across
both revisions, came from that **copy step** itself: two sidecars
colliding on the same stored identity, the copied shape breaking the very
reducer it was supposed to feed unchanged, the copy's size adding to a
shared budget it doesn't own, and a stale copy outliving the file that
produced it.

**Revision 3 removes the copy step.** The board already treats most of
what it shows as a *read-only, derived view* of other evidence - nothing
is "entered into" the board directly (this is the same decision behind
issue #207). So the Planned lane now works the same way: on every board
build, the server lists a small folder of sidecar files and reads them
fresh. Nothing is ever written. There is no `import-plan`, no stored copy,
and therefore no copy to go stale, collide, or exceed anyone else's
budget.

**Background, for why Markdown parsing isn't an option:** PR #264 tried to
read a plan's work-items table straight out of its Markdown. Eight review
rounds showed that this cannot be made safe - every fix closed one way to
hide or fake a row, and every round a reviewer found a new one. That
conclusion is unchanged; only the JSON sidecar's own write path, not its
format, is what this revision reworks.

What you will notice, once this is built: a small JSON file sits next to
each plan a lead wants shown on the board
(`.agenttalk/plans/<plan-id>.items.json`). Add, edit, or delete that file,
and the board's Planned lane reflects it on the very next build - no
command to run, nothing to undo if you remove it.

What you need to do, right now: nothing - review this design. No code
changes are proposed yet.

---

## 0. Decisions carried forward, and what the recast changes about them

Revision 1's six open questions were adopted as decisions. The recast
keeps four of them, and makes two of them no longer apply (removing the
write path removes what they were deciding about):

1. **Size - kept, now per-file only.** `MAX_SIDECAR_BYTES` (512 KiB) bounds
   one sidecar file, checked before it is read into memory (§2).
   `MAX_PLANNED_ROWS` (400) bounds one file's row count. Neither is a
   shared-facts-file budget anymore (§2's "nothing is ever written" - the
   whole aggregate-capacity question revision 2 was still missing an
   answer to no longer has a question to answer).
2. **Empty rows - kept, meaning narrows.** `rows: []` is a sidecar with
   zero rows; it contributes zero Planned cards. There is no
   "guarded replacement" for it to be subject to anymore (§3) - every
   build just reads whatever is currently on disk.
3. **Drift and CI - kept, scope narrows.** `check-plan` (the Markdown
   drift scanner) is explicitly **out of scope, deferred** (§6) - not
   partially specified and then missing from the reuse inventory, which
   is what the second review correctly flagged.
4. **Field vocabulary - kept, unchanged.** `phase`, `owner`, and
   `starts_when` stay bounded free text, not a closed vocabulary.
5. **Schema identification - kept, tightened.** `schema_version` is the
   JSON integer `1` - and revision 3 now refuses any fractional or
   exponent spelling outright (§4), rather than normalizing `1.0` to `1`
   as revision 2 proposed, closing the floating-point-equality gap the
   second review found (`1.0000000000000001` would otherwise round to the
   same float as `1.0`).
6. **CLI transition - no longer applies.** There is no `import-plan`
   command to transition at all under the recast (§6) - the question
   "should it accept a `.md` alias" presupposed a command that no longer
   exists.

---

## 1. Location and identity

Sidecars live at `.agenttalk/plans/<plan-id>.items.json`, one file per
plan, directly inside that one folder (no subdirectories are read). **The
plan's identity is its file name** - `<plan-id>` is the file's stem,
validated as a lowercase slug (`_SLUG`: `[a-z0-9][a-z0-9-]{0,63}`, reused
from `work_board_facts.py:37`). There is no `plan_id` or `project_id`
field inside the file at all: revision 1 and 2 needed those fields to
defend against a value *inside the file* lying about identity or project,
because the file's content was about to be copied somewhere shared.
Revision 3 has nothing to defend there - the file's identity is simply
which path it was found at, inside the one project it was found in, and
that path is never taken from a web request, a sidecar's own content, or
anywhere outside the literal directory listing of
`<project>/.agenttalk/plans/`.

---

## 2. Read, never write

On every board build - the CLI (`agenttalk board ...`) and the console's
own feed/state read path alike - the server:

1. Lists `.agenttalk/plans/` (non-recursive). **A missing folder means no
   Planned lane at all, and no warning** - a project that does not use
   plans yet is the ordinary case, not a degraded one.
2. For each `*.items.json` file found, bounds it **before** reading it
   into memory: a size check (stat, or read one byte past
   `MAX_SIDECAR_BYTES` and stop) refuses an oversized file without ever
   holding its full contents.
3. Validates each file **as a whole** - the full decoding and schema
   contract in §4. **A file that fails produces exactly one warning**,
   shown on the board: `plan <file-stem> could not be read: <reason>` -
   and contributes **zero rows**. A warning is never partial: either a
   file's rows are all used, or none are, and a reason is always named.
   Every build's warnings share the existing `_MAX_PLANNED_WARNINGS`-style
   cap (`work_board_facts.py:855`) across *all* plan files together, not
   per file, so a folder of many broken files still can't grow the
   response past its byte budget.
4. **Nothing is ever written** - not to `work-board-facts.json`, not
   anywhere. This is the central guarantee the recast exists to make:
   reading a plan can never corrupt, replace, or grow anything shared.

**Caching: not proposed.** Every other board-facts reader in this project
(dead letters, attention sources, the Done lane's own facts) already reads
its backing file fresh on every build, with no caching layer. A folder of
at most a few dozen files, each capped at 512 KiB and 400 rows, is cheap
to re-read every time; adding a `(path, size, mtime)` cache would add its
own invalidation-correctness surface for a performance problem nothing has
measured yet. If a future measurement shows this read is actually slow,
caching can be added later without changing anything else in this
document.

---

## 3. Rows: a JSON array, in file order

`rows` is a JSON **array** (not an object map, reverting revision 2's
reshape - see "why", below), each element an object with exactly
`work_item`, `phase`, `owner`, `starts_when`. **The order shown on the
board is the array's own order in the file** - the reader never sorts
it. `work_item` uniqueness across the array is an explicit semantic check
after decoding (a seen-set pass, the same shape PR #264's own parser
already used): a repeated `work_item` anywhere in the array refuses the
whole file, named "duplicate work_item in this plan".

**Why revert the object-map reshape:** revision 2 made `rows` an object
keyed by `work_item` so the decoding contract's duplicate-key rejection
would also enforce uniqueness "for free." The second review found this
breaks the parked reducer (`planned_cards`, which iterates a list and
reads `row["work_item"]`) and the parked row validator (which requires a
list) - porting those unchanged, as §7 intends, needs a list. It also
quietly dropped the file's own row order, which an object's key order
does not reliably promise to preserve across every JSON implementation,
and which this design now states as an explicit contract instead (the
array's order, read verbatim). Trading a list's one semantic uniqueness
check for an object's structural one was not worth either cost.

The planned-card reducer takes the rows array straight from the reader
(§7). Planned-versus-dispatched precedence is unchanged from every prior
revision: a row whose `work_item` has bus activity shows in its real
derived column, never in Planned.

---

## 4. Strict decoding, finished

Builds on revision 1's contract (duplicate JSON keys refused at every
object level via `object_pairs_hook`; `NaN`/`Infinity`/`-Infinity`
refused; strict UTF-8 with no BOM handling needed, since a leading BOM
already fails JSON's own grammar; a named nesting-depth bound of 8,
checked before the parser runs, generous against this schema's own real
bracket depth of 3 - a string value adds no bracket nesting, correcting
revision 2's "depth 4" miscount). Finished this revision:

- **No fractional or exponent numbers at all, anywhere.** `schema_version`
  is the only numeric field in this schema, and it must be the bare JSON
  integer token `1` - no decimal point, no exponent, in any spelling.
  `json.loads`'s `parse_float` hook is set to a function that
  unconditionally refuses whenever it is invoked at all (it is only ever
  called for a token containing `.`, `e`, or `E`); `parse_constant`
  (already refusing `NaN`/`Infinity`/`-Infinity`) is unchanged. This
  closes the second review's finding directly: `1.0000000000000001` is a
  token containing a `.`, so it refuses outright, rather than silently
  becoming the float `1.0` and comparing equal to `1` the way IEEE-754
  equality would otherwise allow.
- **Reject every Unicode `Cc` (control) character in every string value,
  and reject `Cf` (format) characters too** - checked per character via
  `unicodedata.category`. `Cc` (for example DEL, the C0/C1 control
  range) is rejected because none of this schema's fields have any
  legitimate use for a control character. `Cf` (format characters: a
  zero-width joiner, directional marks, soft hyphen) is decided
  explicitly here, not left ambiguous: **rejected** - a field that can
  carry an invisible formatting character is exactly the kind of "hidden
  thing a human reading the board can't see" this whole design exists to
  rule out, the same reasoning that ruled out an HTML comment in a
  Markdown plan. (This check also catches a lone surrogate, category
  `Cs`, in addition to revision 1's own `str.encode("utf-8")` check for
  the same thing - the two checks overlap deliberately; redundancy here
  costs nothing and catches the case two ways.)
- **"Blank" is named precisely.** A string field is blank, and refused
  where non-blank is required, when `value.strip()` is empty - Python's
  own `str.strip()` with no arguments, which uses `str.isspace()`'s
  Unicode-aware definition of whitespace. Named explicitly so an
  independent conformance check (§4's shared cases, below) uses the same
  predicate, not a different one that might disagree on an uncommon
  Unicode space character.
- **One shared conformance list.** Every rule above and in §1-§3 becomes
  one named case in a single list (a checked-in fixture, not restated
  separately anywhere) - each case a minimal input and its expected
  outcome (accept, or refuse with which named reason). The reader's own
  tests run this list, and so does CI, directly - the same list both
  places check against, so the schema, the reader, and CI cannot
  independently drift on what "valid" means the way revision 1's
  schema-versus-importer gap did.

---

## 5. Removing a plan

Delete the file, or move it out of `.agenttalk/plans/`. Its rows leave the
Planned lane at the very next board build - there is no separate "retire"
step, because there is no stored registration left to retire.

**Say this plainly: this is a display, not a record.** The board always
shows whatever is on disk right now; it keeps no history of what a plan
used to say, and the plan's Markdown file - not the sidecar, not the
board - stays the actual durable record of the plan and the reasoning
behind it. See §8 for what follows from this, stated as losses rather than
implied.

---

## 6. Out of scope, deferred

- **`check-plan`** (an advisory Markdown-versus-sidecar drift scanner) is
  not designed further here. The second review correctly found revision 2
  promised this without naming what would build or run it; rather than
  under-specify it again, it is explicitly deferred. A future design can
  propose it once the read-time reader above exists to compare against.
- **No `import-plan`, no `retire-plan`.** The #264 CLI for plans is not
  ported in any form - there is nothing to import into, and nothing to
  retire out of.

---

## 7. Reuse inventory

Only what the read-time model still needs from the parked
`feat/wb-planned-lane` branch (`d3cc6557731a16de295482426681c56db667aefd`;
confirmed by diffing it against current `origin/master` directly, as in
revision 2):

| Piece | Still needed? | How it changes |
|---|---|---|
| `planned_cards` (the dispatched-vs-planned reducer) | **Port** | Unchanged in shape - still takes `(dispatched, planned)` and returns planned cards - but `planned` is now built directly from this revision's read-time scan of `.agenttalk/plans/`, never from a stored, multi-plan section of the shared facts file. |
| `console2.js`'s `planned: 'PLANNED'` label and the planned card's rendering | **Port** | Unchanged - agnostic to where a card's data came from. |
| `work_board_feed.py`'s call site for `planned_cards` | **Port** | Unchanged call shape; its argument now comes from a new reader (below), not `load_planned(store, cfg)`. |
| `work_tags.value("work_item", ...)` | **Reused, already on `master`** | Unchanged - still the row's `work_item` validator. |
| `_SLUG` | **Reused, already on `master`** | Unchanged - validates the plan-id file stem (§1) and, combined with `work_tags.value`, each row's `work_item`. |

**Deleted from the inventory, per the recast (nothing is stored, so none
of these have anything left to do):** `_write_section`'s merge-capable
extension, `_planned_build`, `retire_plan`, `_valid_planned_row`,
`_valid_planned_entry`, `_valid_planned_section`, `load_planned` /
`_load_planned`. A single new function (replacing all of the above for
plans) does the §2 directory scan, §4 decoding, and §3 row validation in
one pass and hands `planned_cards` its input directly. `_read` (the
shared facts-file reader) and `MAX_FILE_BYTES`/`MAX_FACTS` are **not**
part of this inventory at all anymore - plans never touch the shared
facts file, so neither its reader nor its capacity bounds apply to them.

---

## 8. What the recast loses, honestly

1. **No history of past plan content** - only whatever is on disk right
   now is ever visible; nothing is preserved when a sidecar is edited or
   deleted.
2. **A plan shows only where its file currently exists**; a missing or
   broken file blanks that plan's rows until it is restored or fixed,
   with just one generic warning, not a detailed diagnostic trail.
3. **No import "event" at all** - no timestamp of when a plan was
   registered, no confirmation step, no exit code to check; a broken
   sidecar produces one quiet board warning that an operator might not
   notice.
4. **`plan_rev` becomes purely cosmetic text** - there is no enforced
   ordering, no refusal of a repeated or decreasing revision, and no
   rollback guard; two sidecar files can show any revision labels in any
   order.
5. **No protection against an old sidecar file reappearing** (for example
   restored from a backup) - its rows return to the board exactly as if
   freshly written; there is no retirement state it could violate,
   because there is no retirement concept anymore.
6. **Renaming a sidecar's file is indistinguishable from deleting one
   plan and creating a new one** - there is no continuity between the old
   and new identity, unlike an explicit, rename-surviving `plan_id`.
7. **Every board build now does its own directory listing and per-file
   parse**, instead of reading one pre-validated facts file - still
   bounded and small, but strictly more work per build than before.
8. **Two board builds running at the same time can see different
   results** if a sidecar file changes in between - an accepted, benign
   read-consistency gap for a derived view, not a correctness bug, but
   worth naming since the old design's one locked facts file did not have
   it.

---

## 9. Acceptance test

A pinned fixture, not a changing live-store copy (unchanged discipline
from revision 2):

### Positive

1. Write `.agenttalk/plans/board-lanes-and-the-v2-team-views.items.json`
   from the real plan's section 5 (`atk-scratch/lead/plan-board-lanes-v2-views.md`):
   `schema_version: 1`, `plan_name`, `plan_rev: "r2"` (cosmetic, §8), and
   the 7 rows as a JSON array, in the same order section 5 lists them.
2. A fixture store with a **fixed** dispatch history giving exactly two
   of the seven work items a dispatch (`wb-done-facts`,
   `wb-planned-lane`, matching this project's real history, but
   hard-coded into the fixture - not read from the live bus at test
   time).
3. A board build against this fixture must show `wb-done-facts` and
   `wb-planned-lane` in their own derived columns, not Planned, and the
   other 5 rows as Planned cards, **in the file's own order**
   (`wb-lanes-ui`, `v2-team`, `v2-history`, `v2-learning`,
   `v2-walkthrough`).

### Negative

- Every case in §4's shared conformance list gives exactly one warning
  and zero rows from that file - run once as the reader's own tests, and
  once as a CI check against the same list.
- An oversized file (over `MAX_SIDECAR_BYTES`) is refused without ever
  being read into memory whole - assert the refusal happens at the size
  check, before any content read.
- A file appearing and disappearing across three successive builds
  behaves correctly each time: present (its rows show) → deleted (rows
  gone, no warning - a missing file is the ordinary case) → recreated
  (rows return, identical to the first build).
- A build that reads one broken file and one valid file in the same
  folder shows the valid file's rows and exactly one warning for the
  broken one - cross-file isolation, not an all-or-nothing folder
  failure.
- `work-board-facts.json`'s bytes are **byte-identical before and after**
  a board build that reads plans - the read-only guarantee (§2.4),
  asserted directly, not inferred from the absence of an error.

No source changes are proposed in this PR; the above is the acceptance
bar for the follow-up build PR.

## Technical details

- Supersedes the Markdown-reading approach in PR #264
  (`feat/wb-planned-lane`, parked at
  `d3cc6557731a16de295482426681c56db667aefd`) and this document's own
  revisions 1 (`27b872e661fb082a2f6695b9f97fc51ba07de43c`) and 2
  (`9e234e342b46362ef220834deeb72de6b41b6954`).
- Driven by issue #266, issue #265 (now entirely moot for the Planned
  lane under the recast - every #265 item concerned the shared facts file
  plans no longer touch), and two design reviews by
  codex-agenttalk-developer-4: the first (scratch task `tk-bde9e66e8f1a`,
  11 findings on revision 1) and the second, confirm read (scratch task
  `tk-09928b8932bb`, 8 findings on revision 2, 1 P1 + 5 P2 + 2 P3), which
  recommended this recast.
- The recast's motivation cites the work-board decision behind issue
  #207: the board is a read-only, derived view, never a place work is
  "entered into."
- §7's reuse/port inventory is drawn from the same direct `git diff
  origin/master...feat/wb-planned-lane` used in revision 2, re-checked
  against this revision's much smaller surface.
