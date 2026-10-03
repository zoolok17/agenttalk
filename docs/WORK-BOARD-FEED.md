# Work-board feed reference

Audience: console integrators consuming the read-only bus-derived board.

`GET /api/work-board?root=<project-id>` selects a configured root using the same
rules as the other console feeds. Unknown roots return HTTP 400. The response is
JSON, with no cursor or history endpoint. It never publishes work or grants merge
authority. The shared snapshot worker computes the projection; polling does not
read message files, run Git, or query external CI.

Schema version 1 includes `target_root_project_id`, `generated_at`, `coverage`,
`items`, `legacy`, `unassigned`, `total_count`, `truncated`, `omitted_count`,
`errors`, and `window_days`. During initial discovery, coverage is `building` and
the total is unknown. After a refresh failure, available cards are `last_known`;
consumers must show degraded coverage and must not present these as current Ready.

The feed includes active items and Done items whose approximate last work event
falls within seven UTC days. Entirely cancelled/declined work without remaining
obligations is excluded only when its history is complete. Unknown items remain
visible. Display order uses event time and item key, not causal workflow order.
Done needs integration evidence recorded by the lead-run `agenttalk board
verify-merges` (below); task completion alone never means Done. External CI is
labelled `local checks not tracked`.

A plan row with no dispatch appears as Planned (`workflow_column: "planned"`),
recorded by the lead-run `agenttalk board import-plan` (below). A dispatched
item always keeps its own derived column instead; it is never shown as
Planned too. Planned cards are appended after every dispatched card and are
the first ones cut by the 100-card/256 KiB bounds, so a plan can never push
active work out of the response.

There are at most 100 tagged cards and 256 KiB of UTF-8 JSON, including groups and
coverage. Overflow omits whole cards, sets `truncated`, and reports known totals
and omissions; incomplete totals are null. One counted Legacy group and the
unassigned group sit outside the card count. Oversized group details can be omitted
with `groups_truncated`. A card too large to fit is counted as omitted.

Coverage budgets the selected history across active and compacted partitions,
including linked untagged threads, before applying the card limit: 50,000 envelopes
and 128 MiB source bytes. At 60% it warns to schedule archive indexing. Overflow
retains last-known placement with `capacity_exceeded`; it cannot certify a partial
reduction. Coverage also reports scan times, valid-until, generation and discovery
statistics. Freshness expires 15 seconds after scan start.

Local checks use `wb.<item>.c<cycle>.<check>` with check keys matching
`[a-z0-9-]{1,24}`, scope `<project-id>/<item>/c<cycle>`, and the exact full candidate
revision. Only validated blocker-green evidence satisfies a required check. Missing
revisions, wrong scopes, advisory greens and another item's gates cannot satisfy it.
Global barriers remain separately visible on cards and keep their ordinary effect
on `check --gates`.

Root checks, non-board scoped checks, and both attention feeds exclude `wb.` gates.
Adding these names to root `required_gates` is refused. Legacy root-list entries
are ignored with a configuration warning. Malformed gate state still fails closed.
Board-gate cleanup is separate future maintenance, never a GET side effect.

## Integration facts and the Done lane

A card reaches Done only when its reviewed candidate is proven merged into an
approved target. A lead-run command makes that proof outside the web server and
records it in a facts file. The snapshot worker reads the file and never runs
Git. This replaces the in-server cached observer of design section 4 (the
operator's decision of 2026-09-30). Section 4's alias allowlist and probe rules
still apply, inside the command.

### Setup

`work_repos` in the project config is an access allowlist of operator-approved
local checkouts, not an item registry. For this repository:

```json
"work_repos": {
  "agenttalk": {
    "path": "D:\\Projects\\Claude\\agenttalk",
    "targets": ["refs/heads/master"],
    "default": true
  }
}
```

- An alias is a lowercase slug.
- `path` must be the canonical, absolute top of a non-bare, non-shallow work
  tree: no `..`, symlink, junction or other spelling of it.
- `targets` are distinct full refs under `refs/heads/` or `refs/remotes/`. A
  short name such as `master` is refused, because a tag could shadow it.
- A dispatch may declare `work_repo` (an alias) and `work_target` (`master` or
  `refs/heads/master`). An item without a declaration uses the one alias marked
  `default`. With no default, an unlisted alias or conflicting declarations,
  the item stays Unknown. An explicitly empty `work_repo` or `work_target` is
  an invalid binding ("empty repository/target declaration"), never the same
  as no declaration. Paths never come from messages.
- Invalid entries, two aliases for one checkout, and two defaults are refused
  with a warning.

Each card carries `repo_binding`: `null`, `{repo, target}` or `ambiguous`.

### Command

`agenttalk board verify-merges [--dry-run] [--json]` reduces the board
read-only, over active and compacted envelopes. It checks each (work item,
candidate) pair against each approved target of the item's alias:

- Only `rev-parse`, `merge-base --is-ancestor` and `cat-file` run: shell-free,
  with validated arguments.
- Inherited `GIT_*` variables are dropped. `GIT_NO_LAZY_FETCH=1`,
  `GIT_NO_REPLACE_OBJECTS=1`, `GIT_TERMINAL_PROMPT=0`,
  `-c core.fsmonitor=false` and `--no-replace-objects` are set. No hooks,
  fetch, status, diff, filters or textconv are used.
- Probes run one at a time, with a 2 second timeout each. The first timeout
  abandons that checkout for the rest of the run.
- A target must resolve to exactly its configured ref, with no ambiguity
  warning.
- A shallow checkout, a missing candidate or a failed ancestry check records
  no fact (Unknown).

One run at a time: a publishing run holds `.agenttalk/work-board-verify.lock`
(outside `state/`, so a reset cannot remove it) from its first read to its
publish, so two runs never interleave. A second run is
refused at once with "another verify-merges run is in progress". A dry run
publishes nothing and takes no lock. The snapshot worker never takes this lock.

The command lists unknown pairs. It exits 2, having published nothing, when:

- no usable alias is configured (so clearing `work_repos` never erases the last
  proof; delete the facts file to retire it);
- another run is in progress;
- the store session changed during the run (a reset);
- the facts file has an unsupported schema version, or exceeds the 512 KiB read
  bound (either way the file is left untouched).

Otherwise it exits 0.

### Facts file

`.agenttalk/state/work-board-facts.json` (schema version 1) is replaced
atomically under `state/work-board-facts.lock`; other sections of the file are
kept. It holds at most 400 facts and 512 KiB. Each fact records `project`,
`repo_alias`, `repo_path`, `work_item`, `candidate`, `target_ref`, `target_oid`
(at check time), `checked_at` and `result` (`integrated` or `not_integrated`).
Each run rewrites the integration section for the current pairs, so an
invalidated result disappears. The section records the store `session_id` its
run started in. The run publishes only if that session is still current, and
the reader ignores a section from another session, with a warning.

The snapshot worker reads the file on every refresh, bounded and schema-checked:

- A missing, unreadable, malformed or oversize file gives no facts and a warning
  in `errors`. A missing file is silent only while `work_repos` is not
  configured. Any other fault while reading or selecting the evidence also
  becomes a warning. The facts never error the board and never imply Done.
- The file existing with no `integration` section at all (for example, only
  `import-plan` has ever run against this store) reads the same as a missing
  file: silent while `work_repos` is not configured, else a warning saying
  evidence is missing — never the `malformed` warning, which is reserved for
  a section that IS present but does not parse as valid facts.
- A fact whose project, alias path or target no longer matches the config is
  ignored, with a warning, so old proof is never reused after a remap.
- A fact counts only for the item's CURRENT binding: its declared `work_repo`
  and `work_target` (from its current cycle), else the current default alias
  and its targets. Proof from an earlier default or an earlier target is never
  reused. Unmatched facts leave the card's column as derived and end its reason
  with `integration evidence ignored: <why>`.
- A fact older than `integration_facts_max_age_seconds` (default 86,400; from
  60 seconds to 30 days), or dated after the reader's clock (writer and reader
  run on one host), is stale. It never makes Done: the card keeps its
  derived column, its reason ends with `integration evidence stale (as of
  <time>)`, and `integration_stale_as_of` holds the time.
- A fresh `integrated` fact for the current candidate places the card in Done,
  subject to every Ready condition. The card's `integration` maps the candidate
  to `{repo_alias, target_ref, target_oid, checked_at}`. Fresh
  `not_integrated` facts map it to `false` only when they cover every target
  the item selects. A partial negative (another target timed out or was not
  checked) stays Unknown, and the reason ends with `integration evidence
  incomplete (no fresh result for <targets>)`.

### Refresh owners

The lead runs `agenttalk board verify-merges` right after every merge and on
every lead tick. Nothing else refreshes the facts, so without it Done goes stale
within the window.

Ordinary merges and fast-forwards are proven by ancestry. Squash and rebase
merges stay unproved.

## Planned work and the Planned lane

A PLANNED lane shows work that is in a plan but not yet dispatched. The input
is the markdown plan format the lead already writes: a `# Plan: <name>` title
line, a `Plan revision: rN` line, and a `## 5. Work items` table. This is a
plain, versioned, portable contract — any plan file in this shape is
understood, without needing the lead's own private template
(`plan-template.md`, kept outside this repository).

Two commands write it (`import-plan`, `retire-plan`, below) and one design
principle governs both, and reading the result back: **fail closed on
anything unexpected, and bound every output.** Concretely: parse exactly one
table, not everything that looks table-shaped in the section; refuse a write
rather than silently treat a oddly-shaped stored section as empty; capture
the session once, before any read, and re-check it right before the write;
and cap what a corrupted store can make the board say, so it can never push
real cards out of the response.

### Commands

`agenttalk board import-plan <file>` reads one plan file and records its
section 5 rows in the facts file wb-done-facts created above
(`.agenttalk/state/work-board-facts.json`), in their own `planned` section.
The existing `integration` section, and every OTHER plan's rows, are left
exactly as they were. It never runs Git and never touches the bus.

The whole file is first split into lines and classified ONCE, by ONE
function: every line is exactly a heading, a title (`# Plan: ...`), a fence,
a table row (starts with `|`), a `Plan id:`/`Plan revision:` field, or plain
text. Every rule below — the section-5 location, the header block, the
fence ban, the field counts, the table — works ONLY on that classified
list; nothing re-searches the raw text. Seven review rounds each found a
different pair of ad hoc regexes quietly disagreeing about what a heading
or a field was (`##5. Work items` passed one check and failed the next); one
classifier removes the possibility of two rules disagreeing, because there
is only one rule for each kind of line.

**Near-miss refusal:** a line starting with `#` that is not a valid
CommonMark ATX heading (`#` through `######`, then a space, then content —
so `##5. Work items` and `#Plan: ...` are NOT headings) refuses outright,
naming the line. A line that looks like a `Plan id:` or `Plan revision:`
field (any case, optional spacing, a colon) but whose value is not valid
ALSO refuses outright, naming the line — it is never silently treated as
plain text or quietly skipped. This closes the class of bug where "only
count VALID occurrences" let an invalid second occurrence go unnoticed.

The plan file must contain EXACTLY ONE heading classified as `## 5. Work
items`. Zero or more than one (including one hidden inside a fenced
example) refuses. No line classified as a fence may appear anywhere from
the start of the file through the end of section 5 (the next heading line
after it, or EOF) — one anywhere in that range refuses, whether or not it
"closes". A fence AFTER section 5 is never examined and is fine. Section
5's table is then the FIRST CONTIGUOUS run of table-row lines directly
under the heading (leading non-table lines, like the template's own
"Dispatches carry ..." sentence, are skipped; the table ends at the first
line after it that is not a table row). Anything past that — a second
table further down the section (a "Legend:", say) — is never read, by
construction; it is not a reason to refuse.

The SAME "exactly right, or refuse" principle applies to every other field
the importer reads, not just the section-5 heading. The title, the optional
`Plan id:` line and `Plan revision: rN` are read ONLY from the plan's
HEADER BLOCK — the lines before the file's first heading line (section 5's
own heading counts, and so does any other heading earlier in the file).
Each one's classified occurrences are counted across the WHOLE file: the
title and revision must occur exactly once, `Plan id:` at most once, and
EVERY occurrence — not just the ones that happen to match — must sit
inside that header block. A plan with an otherwise-normal section 5 and an
ALLOWED fenced example after it (fences after section 5 are fine) that
happens to contain its own `Plan id: ...` line used to let that line win,
importing under a hijacked, unrelated id and overwriting whatever already
used it; it is refused instead now, wherever the stray occurrence sits.
The table's own header is checked the same way: a required column name
appearing twice (two `work_item` columns, say) refuses rather than silently
using the first one.

It exits 2, having published nothing, when the plan file:

- does not exist, is unreadable, is not valid UTF-8, or exceeds the 512 KiB
  read bound;
- has a line starting with `#` that is not a valid heading (needs a space
  after the `#`s), or a line that looks like a `Plan id:`/`Plan revision:`
  field but has an invalid value — either refuses immediately, by itself,
  wherever it is;
- does not contain EXACTLY ONE `# Plan: <name>` title line, or one exists
  outside the header block;
- has an explicit `Plan id:` line that is not a lowercase slug of at most 64
  characters, occurs more than once, or sits outside the header block (see
  **Plan id** below), or (with no explicit line) a title that yields no
  usable slug;
- does not contain EXACTLY ONE `Plan revision: rN` line, or one exists
  outside the header block;
- does not contain EXACTLY ONE `## 5. Work items` heading line;
- has any code fence from the start of the file through the end of section 5;
- has no table directly under the heading, or the table's second line is
  not a separator row of the same width as the header, or the table header
  is missing the `work_item`, `Phase`, `Owner (vendor)` (or `Owner`) or
  `Starts when` column, or repeats any of those required columns;
- has a table row whose `work_item` is not a lowercase slug of at most 64
  characters, or that repeats a `work_item` already seen in this plan's own
  table;
- derives its plan id from the title (no explicit `Plan id:` line) and that
  id already names a DIFFERENT plan's stored rows (a title collision — see
  **Plan id**);
- is a non-empty table where EVERY row was skipped (see below) — this is
  never the same as an intentionally empty table, and never silently clears
  the plan;
- would leave the facts file over its 512 KiB size bound;
- finds the CURRENTLY STORED `planned` section present but not in exactly
  the expected shape (every container level type-checked — see **Fail
  closed on the stored shape** below), or finds the store's session has
  changed since the command started (a concurrent reset).

A table row is SKIPPED, with a reason, rather than failing the whole import,
when it has an empty `Phase`, `Owner` or `Starts when` cell, or the wrong
number of cells for the header (a markdown authoring slip, or an unescaped
`|` inside a cell — see **Escaped pipes** below). The rest of the plan still
imports around it, as long as at least one row survives; the command lists
every skipped row (and so does the `--json` output, under `skipped`). A
table with a valid header and separator and ZERO data rows FROM THE START is
a valid, empty plan — this is how a plan's rows are cleared without retiring
it outright. A table that had rows but lost every one of them to skipping is
NOT the same thing: that refuses (see above), it never clears silently.
Otherwise the command exits 0.

`agenttalk board retire-plan <plan id>` atomically removes one plan id's
rows from the `planned` section — for a plan that is done, abandoned, or
renamed without keeping its old id. It exits 2, publishing nothing, if no
rows are currently recorded for that plan id, the stored section is present
but not in the expected shape, or the session has changed since the command
started. Every other plan id's rows and the `integration` section are
untouched either way.

### Fail closed on the stored shape

Before EITHER command writes anything, the planned section's CURRENT stored
value (if any) is validated at every container level: the schema version,
the `plans` map itself, every plan id (a lowercase slug) and every entry in
it (exact key set, non-empty string fields, a valid `rows` list). A section
that is present but does not match this exactly — a `plans` container that
is a list instead of a map, say — refuses the write outright. It is never
silently treated as "empty" and overwritten: that would durably discard
whatever it held, which looks identical to the data simply never having
existed. The server's own read side (below) stays more forgiving for
DISPLAY — one malformed plan there is dropped and the rest of the board
still renders — but a WRITE never guesses at a shape it cannot verify.

### Session capture

Both commands capture `(project, session)` once, before reading anything —
the plan file, the facts file, or the store's roster. The read, the build of
the new section value, and the publish all happen under the one facts lock.
Immediately before the atomic replace, the session is re-checked against a
fresh read; if it no longer matches what was captured at the start — a
concurrent `agenttalk reset` landed anywhere in between, including between
the facts file's own read and this check — the write is refused and nothing
is published. This is the same mechanism `verify-merges` already used for
the `integration` section, now shared by both sections through one explicit
parameter on the shared writer, rather than two different ad hoc checks.

### The import contract

- **Schema version:** the `planned` section carries its own
  `schema_version` (currently 1), checked independently of the facts file's
  own top-level `schema_version`. A section with an unsupported version is
  left untouched and the import (or retirement) is refused, never silently
  reset.
- **Plan id:** an OPTIONAL `Plan id: <id>` line, if present, names the slug
  that IS the plan's identity (validated the same way a `work_item` is: a
  lowercase slug of at most 64 characters). It survives a later title
  change — rename the plan freely, re-import under the same `Plan id:` line,
  and its rows stay the same entry.
  Without a `Plan id:` line, the id falls back to the title's own slug
  (legacy): the title's text after `# Plan: `, lowercased, with every run of
  characters outside `a-z0-9` collapsed to one `-`, trimmed of leading and
  trailing `-`, and capped at 64 characters. Renaming the FILE never changes
  this fallback id; changing the TITLE does — so a renamed plan, re-imported
  under its new title with no `Plan id:` line, becomes a new, separate entry,
  and the old one is only cleared by `retire-plan` (or by the next import
  that happens to collide, see below).
  **Collision guard:** two different plans whose titles happen to slugify to
  the SAME id, with neither using an explicit `Plan id:` line, do not
  silently overwrite one another — the second import is refused, naming the
  stored plan's own title and suggesting an explicit `Plan id:` line. Giving
  either plan its own `Plan id:` line resolves it.
- **Plan revision:** the literal `rN` token from the `Plan revision:` line
  (everything after it — "supersedes", a status, an owner — is ignored).
- **Row identity:** `(plan id, work_item)`. A re-import of the same plan id
  REPLACES every row of that plan id atomically: a row dropped from the file
  disappears from the board, and a superseded `plan_rev` replaces the old
  one. A different plan id's rows are a separate entry and are never touched
  by another plan's import or retirement.
- Each row records `work_item`, `phase`, `owner` and `starts_when` — the
  table cell text, verbatim (the owner cell's vendor parenthetical is kept
  as-is, never parsed further). A literal `|` inside a cell must be escaped
  as `\|`, exactly as in ordinary GitHub-flavoured markdown tables; otherwise
  it would end the cell early and misalign everything after it.

### Facts file: the `planned` section

```json
"planned": {
  "schema_version": 1,
  "session_id": "<the store session this was last written under>",
  "plans": {
    "board-lanes-and-the-v2-team-views": {
      "project": "<project id>",
      "plan_rev": "r2",
      "plan_name": "board lanes and the v2 team views",
      "written_at": "2026-10-01T00:00:00+00:00",
      "rows": [
        {"work_item": "wb-planned-lane", "phase": "1",
         "owner": "claude-agenttalk-developer-6 (claude)",
         "starts_when": "wb-done-facts merged"}
      ]
    }
  }
}
```

The snapshot worker reads this section on every refresh, bounded and
schema-checked exactly like `integration` above: a missing, malformed or
wrong-schema section gives no planned rows and one `errors` warning, never a
broken board. A plan whose `project` does not match the current store is
excluded without a warning (it belongs to another project's facts file, nothing
changed here to report). Unlike the write path's all-or-nothing shape check
above, reading is per-plan: one malformed stored plan entry is dropped with
its own warning, and every OTHER plan still loads and still shows on the
board. However many plans are malformed, the warning list is capped (the
first 5, then one "and N more" line) — a corrupted store with thousands of
bad entries can never grow this list large enough to push real cards out of
the response's byte budget. Planned cards themselves carry the same
protection from the other direction: they are appended after every
dispatched card, so the response's existing 100-card/256 KiB bounds always
cut Planned cards first, never active work.

### How a plan row becomes a card

For every plan row whose `work_item` has **no dispatch at all** (no bus
opener ever carried that `work_item` tag, dispatched, done, cancelled or
otherwise):

- exactly one plan names it: a Planned card, carrying `planned: {phase,
  owner, starts_when, plan_id, plan_name, plan_rev}`;
- more than one plan names it: an Unknown card, reason `planned in more than
  one plan: <plan id>, <plan id>, ...` — never a silent pick of either plan's
  row.

A `work_item` with any dispatch is never shown as Planned: the dispatched
item keeps exactly the column the reducer already placed it in, Planned or
not. Planned and conflict cards carry no obligations, candidate, checks or
integration evidence — there is no execution history to show yet.

### Refresh owners

The lead runs `agenttalk board import-plan <file>` after approving or
revising a plan (section 8 of the plan template's replan rule), and
`agenttalk board retire-plan <plan id>` once a plan is fully done or
abandoned. Nothing else refreshes or clears planned rows, so a merged or
abandoned plan stays on the board exactly as last imported until the lead
re-imports (with fewer rows, or an empty table) or retires it.
