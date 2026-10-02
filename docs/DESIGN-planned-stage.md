# The Planned lane, from bus records

Status: proposed, revision 2. Date: 2026-10-02.

## In plain words

The work board shows work in progress, in review, and done - nothing
planned but not started. Three earlier attempts to add that (reading a
plan's prose, a JSON sidecar, in three design rounds) were all parked:
each invented its own file format with its own decoding, identity, and
storage problems.

This design invents no file. The lead writes ONE record per planned item
to itself, on the same bus log the board already scans, carrying the
same two labels a real `task` already carries (`work_item`, `work_title`).
The board shows the item under Planned until a real `task` or
`review-request` for the same `work_item` appears - permanently; after
that, it never returns to Planned under that name.

**Revision 2** closes a first design review's seven findings by removing
three things that caused nearly all of them: broadcasting the record,
trusting message order, and three optional fields. What is left is
smaller and exact.

What you will notice, once built: one command per planned item; a
Planned lane on the board. What you need to do now: review this
contract. No code is proposed yet.

---

## 1. One self-addressed record, one publishing route

**Self-addressed, not broadcast (closes F2b/F7c/F9).** The lead sends a
`planned` record to itself - sender and recipient are the same agent, the
same pattern `current_epoch()`'s global barrier already uses
(`store.py:4879`: "a single self-addressed barrier is globally
authoritative because this scans the WHOLE validated log"). One copy,
one file, written once. This removes every broadcast concern at a stroke:
no partial fan-out, no `--resume`, no second seat's wrapper turn, no
delivery or dead-letter path - there is no second party to deliver to.
The board finds it by scanning the validated log (`store.valid_messages()`
/ the snapshot `work_board_feed.build` already reads), the same way it
finds every other message, not through anyone's inbox.

**One publishing route (closes F3).** Only `agenttalk board plan
add|change|withdraw` may send `kind=planned`, the only caller of one
shared planned validator (§3). `send`/`broadcast` refuse `--kind planned`
outright, the same refusal `send` already gives `task`/`rescind`/`end`
(`cli.py:1722`). **Trusted direct `Store.send()` callers are not
specially defended, and this is not a new gap:** `Store.send()` validates
only `kind in KNOWN_KINDS` (`store.py:3488`); no kind's semantic rules,
`task`'s included, live inside `Store.send()` itself today. A direct
caller already bypasses `task`'s role/version checks the same way.

**Replay is structural only.** A stored `planned` record is honoured if
its *shape* is valid (§3) and, when the store can say who was lead at
that point in history, its sender held that role then. Where history
cannot establish that (no roster-at-time reconstruction available), the
record is honoured on shape alone - the same "judge history by
publication-time rules, not today's roster" principle `work_tags.inherit`
already applies to replay. This is a policy choice, stated once, not
reinterpreted per record.

---

## 2. Identity: `replaces`, not order (closes F2a)

Every `change` or `withdraw` record names **exactly one** earlier record
it replaces: `meta.replaces = <message id>`, validated to be an earlier
`planned` record for the **same** `work_item`, from a lead (§1). The
*current* plan for a work_item is whichever record nothing else replaces.
**No timestamp, no list position, no publication index decides this** -
`replaces` is the only link. Two records for one item that both go
unreplaced make that item **Unknown in Planned**, with one bounded
reason ("competing unreplaced plan records") - never a guess at which one
is "latest."

This also answers finding F2's broadcast-copy question without needing
an answer: there is one copy (§1), so "which copy is newest after
compaction or resume" cannot arise.

---

## 3. v1 fields, and the exact metadata table (closes F3, F8)

`work_item`, `work_title`, and `withdrawn` (boolean, withdraw only).
**`owner`, `phase`, `starts_when` are deferred** - not part of v1. No new
keys join `work_tags.FIELDS`; the planned validator owns `work_item`
(reusing `work_tags.value("work_item", ...)` unchanged) and
`work_title`/`withdrawn` itself, so **this does not touch `FIELDS`
validation for any other kind** (a real compatibility risk the first
review found: adding global keys like `owner` could reject metadata on
ordinary messages that happened to use that name).

**Nonblank, defined exactly, for `planned` records only:**
`value.strip() != ""`. `work_title`'s own existing rule (nonempty, ≤160)
is unchanged for every other kind - this is a new, separate check the
planned validator applies to its own copy of the field, not a change to
`work_tags.value`.

| Action | `work_item` | `work_title` | `replaces` | `withdrawn` |
|---|---|---|---|---|
| `add` | required (no current record may already exist for it) | required, nonblank | absent | absent |
| `change` | required (must name an item with a current record) | required, nonblank | required: the current record's id | absent |
| `withdraw` | required (must name an item with a current record) | absent | required: the current record's id | required: `true` |

`board plan add` and `board plan change` each resolve "the current
record for this `work_item`" themselves and refuse with a clear reason
when the precondition is wrong (`add` on an already-planned item: "already
planned; use `change`"; `change`/`withdraw` on one with no current
record: "not currently planned; use `add`") - the advertised
`add|change|withdraw` surface and the `replaces` mechanics agree by
construction, not by convention.

---

## 4. Isolation from real work (closes F4, F7a)

**`_audit`/`_audit_one` gain one new, first-checked branch:**
`if m.kind == "planned": return` - no flag, no `owners()` call, no
attribution to any item. Today, `_audit_one` runs for *every* message
(`work_board.py:186`), and a message with a dangling `in_reply_to` gets
flagged against whatever `owners()` can attribute it to, including its
own `work_item` tag (`work_board.py:176-177`) - so an unrelated
`planned` record with a stray `in_reply_to` could otherwise inject a
phantom entry, or a spurious issue, onto a real item. The new branch
removes `planned` from `_audit` entirely; it is validated by its own
read-side pass (§3's table, applied the same way at read time as at
publish time) in `work_board_feed.build`, never by the reducer's
request/reply machinery.

**A malformed *current* record makes the item Unknown in Planned, never
a fallback to an older one.** A structurally invalid *envelope* (bad
schema/signing) still only increments `invalid_count`, unchanged
board-wide coverage behavior; a structurally valid envelope whose
`planned` metadata fails §3's table is never a plan, not even a stale
one - one bounded diagnostic, zero Planned rows from it, every other
item unaffected.

**Activity exclusion.** `work_board_feed.build`'s activity loop
(`work_board_feed.py:87-104`) currently appends to `activity[slug]` for
*any* message resolving to a slug, regardless of kind - a `planned`
record for an old Done item would refresh its `last_work_event_at` and
could keep it out of the 7-day prune. `planned` is now excluded from that
loop entirely: it is never activity, and can never move, refresh, or
resurrect a Done or any other real card.

---

## 5. Permanent promotion (closes F1)

Once any `task`/`review-request` exists anywhere in the log for a
`work_item`, that item **never shows as Planned again** - including
after its Done card ages off the board's 7-day window. "Promoted" is a
property of the reducer's full history
(`work_board.reduce(...)["items"]`/`"known"`), never of what is
currently *displayed* - display expiry is not a new planning decision.
To plan the same conceptual work again, use a fresh `work_item`.
`board plan add` enforces this directly: it refuses, naming the existing
work, when the given `work_item` already has any real opener in history -
so the lead learns the rule at the moment they would have broken it, not
by reading this document.

**Coverage caveat:** `add`'s refusal check, and promotion generally, can
only be as complete as the reducer's own view of history. When archive or
coverage history is incomplete, "no opener exists for this `work_item`"
is never asserted - the item is Unknown, the same honest treatment the
rest of the board already gives incomplete coverage.

---

## 6. Budget and wire shape (closes F5)

`bounded()` (`work_board_feed.py:40`) applies one 100-card/256-KiB limit
to `feed["items"]` - real board cards only. Planned cards are a
**separate wire field**, `feed["planned"]`, never appended to `items` and
never competing for its budget. Its own limit: **20 cards, named**,
computed before `bounded()` runs; past 20, an **overflow count** (an
integer, lane chrome - never a fake `work_item` card). `bounded()` gains
one new trimming step, run *before* its existing items-trimming loop: if
the whole response is over `byte_limit`, drop planned cards (then
diagnostics) one at a time until under budget or `planned` is empty -
**`feed["items"]` is never touched until `planned` already is.** Planned
diagnostics (§4) share this same small allowance, bounded in count and
bytes before they ever reach the trimmer - not appended to the unbounded
`errors` list.

`bounded()` is the one function both the fresh-build path and
`SnapshotService.board()`'s last-known/stale re-bounding call
(`envelope_snapshot.py:344`) already share; teaching `bounded()` about
`planned` covers both call sites with no separate change at the
last-known path.

---

## 7. Mixed versions: an honest, undetected limit (closes F6)

Only the lead ever receives a `planned` record (§1), so there is no
*recipient* whose version could be behind - `task`'s roster-health check
(`_recipients_behind_kind`) does not apply, and `--force` is never needed
to publish. The real risk is an **old board reader**: any `agenttalk
board` run or `agenttalk serve` process built before this feature. It has
no per-process version advertisement this codebase can inspect
(`_recipients_behind_kind` reads *agent* health, not a *board server's*
capability) - **this cannot be detected automatically, and this document
says so rather than promising a check that doesn't exist.** The rollout
is a release-floor statement, not a runtime gate: upgrade every board
reader before relying on the Planned lane. An old reader that scans a
`planned` record it cannot parse rejects the envelope, degrading
`selected_closure` to `status="incomplete"` and marking every item
`unknown` - already true of adding any new kind, not specific to this
one, which is why the floor is a release note, not an afterthought.

---

## 8. Reuse

**Port:** the PLANNED label from `console2.js`. **New, not a port** (the
first review correctly flagged this): the board model and card renderer
must learn `withdrawn`'s absence-means-active shape and the `planned`/
`overflow_count` wire fields (§6) - v1 has no owner/phase/starts-when to
map, since those are deferred (§3).

**Do not port:** anything that reads a plan file - the Markdown parser,
every JSON-sidecar reader/validator from #274's three revisions,
`_write_section`'s merge extension, `_planned_build`, `retire_plan`, and
revision 1 of this document's order-dependent "planned minus dispatched"
merge (superseded by §2's `replaces` links and §5's permanent
promotion).

---

## 9. Acceptance cases

1. Add, change (via `replaces`), withdraw - each produces the right
   Planned-lane state, in order.
2. Promotion: a `task` for a planned `work_item` removes it from Planned,
   permanently - including after its Done card later ages off (§5).
3. Shuffled input: a store whose scan order differs run-to-run (archive
   compaction, concurrent publication) produces the identical Planned
   result - nothing depends on list position (§2).
4. Competing replaces: two records both naming the same `replaces`
   target make that item Unknown, with the named reason.
5. A malformed *current* record: one bounded diagnostic, the item
   Unknown in Planned, no fallback to its prior valid record, every other
   item unaffected.
6. A busy board: 100 real cards plus 25 planned - 20 planned cards show,
   a `5` overflow count, and all 100 real cards are untouched.
7. Oversized diagnostics: enough malformed planned records to threaten
   the byte budget still leave `feed["items"]` intact (§6).
8. Incomplete archives/coverage: `add`'s promotion-refusal check and the
   Planned lane both report Unknown, never a false "nothing real exists
   for this item."
9. The `SnapshotService.board()` last-known/stale path renders `planned`
   and its overflow count identically to a fresh build.
10. An old-reader rollout: a build predating this feature scans a log
    containing a `planned` record, rejects the envelope, and (today's
    existing, unchanged behavior) shows `status="incomplete"`/items
    `unknown` - asserted as the documented, known limit (§7), not a
    surprise.

---

## 10. Open questions

1. **Should `replaces` require the exact message id, or also accept a
   short form** (e.g., "the current record for this `work_item`",
   resolved by the CLI) for a lead typing the command by hand?
   Recommendation: the CLI resolves and fills `replaces` automatically
   (§3) - a human never types a message id; the stored record always
   carries the exact id.
2. **Does a withdrawn item's `work_item` ever become reusable for a
   fresh, unrelated plan** (never dispatched, only ever withdrawn), or
   does §5's "promoted work_items are permanent" also apply to a
   withdrawn-but-never-dispatched one? Recommendation: no - `work_item`
   identity is permanent once used for *any* planned or real record, to
   keep one rule ("a `work_item` means one thing, forever") instead of
   two.

## Technical details

- Supersedes revision 1 of this document, #266, and the parked PR #264 /
  PR #274.
- Driven by issue #279 and a first design review
  (codex-agenttalk-developer-4, `tk-86567e72cad1`): RESHAPE, 7 P2 + 2 P3,
  no P1, no inline GitHub comments on the reviewed head. No connector
  comments to answer this round.
- Code cited: `src/agenttalk/store.py` (`KNOWN_KINDS`, `send`,
  `current_epoch`), `src/agenttalk/work_tags.py` (`FIELDS`, `value`,
  `OPENERS`), `src/agenttalk/work_board.py` (`_audit`, `_audit_one`),
  `src/agenttalk/work_board_feed.py` (`build`, `bounded`),
  `src/agenttalk/envelope_snapshot.py` (`SnapshotService.board`,
  `selected_closure`), `src/agenttalk/cli.py` (`cmd_send`, `cmd_task`,
  `_recipients_behind_kind`) - all read at `origin/master`, `4e8cf71c`.
