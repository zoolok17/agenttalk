# The Planned lane, from bus records

Status: accepted. Part 1 (the publish path and the state machine below) is
built in this PR; part 2 (the feed projection, the console lane, and the
rollout note) is not yet built - §3's budget is recorded now for part 2 to
build against. Date: 2026-10-02.

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

Two design reviews later, the confirm-read found no remaining P1s and
asked for two decisions (the state machine, §2; the byte budget, §6)
before building - both are made and recorded below.

**This PR is part 1: the publish path and the state machine.** You can
run `agenttalk board plan add|change|withdraw` today. Part 2 (the feed
projection and the console lane that actually show a Planned column) is
not yet built - see §6 and §9's part-2 cases.

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

**Replay is structural only (final decision, F7).** A stored `planned`
record is honoured on *shape alone* (§3's validator) - replay **never**
queries whether its sender held the lead/liaison role at the time, or
holds it now. Authority is enforced ONLY at the publishing boundary
(`agenttalk board plan`, §1's one publishing route); once a record is in
the validated log, re-deriving "was this sender authorized back then"
would need a roster-history reconstruction this codebase does not have,
and re-checking against *today's* roster would let a role change silently
rewrite history's meaning - the same reason `work_tags.inherit` judges a
reply against its *opener's* recorded fields, never today's config. One
policy, stated once, implemented by `work_tags.validate_planned` /
`work_board.planned_state` never reading `m.sender` for anything beyond
identifying the self-addressed pair.

**Self-addressing IS checked structurally at replay too (fix round 1,
F9).** This is not a role-authority re-check - it is the record's own
*shape*: `sender == recipient` is as much a part of a well-formed
`planned` record as `work_item`/`work_title` are, so
`work_tags.validate_planned(meta, sender=..., recipient=...)` runs this
check on every replay the same way it runs at publish, and a stored
record from the lead to a second agent (a pre-fix log entry, or a hand
edit) is treated as malformed - the item is Unknown, never silently
accepted as active.

---

## 2. Identity: `replaces`, not order - the state machine (closes F2)

Every `change` or `withdraw` record names **exactly one** earlier record
it replaces: `meta.replaces = <message id>`, validated to be an earlier
`planned` record for the **same** `work_item` (§1: never role-checked at
replay). **No timestamp, no list position, no publication index decides
anything** - `replaces` is the only link, and the CLI resolves it itself
(below), never a human typing a message id by hand.

### Five states, per `work_item`

- **absent** - no `planned` record and no real opener exist. Not a key in
  `planned_state`'s output (§7's docstring) - implicit.
- **active** - exactly one rooted, acyclic, same-item chain of valid
  `planned` records resolves to one tip (a record nothing replaces), and
  the tip is not a withdrawal. Carries the tip's `work_title`.
- **withdrawn** - the chain's one tip has `withdrawn: true`. **Terminal.**
- **promoted** - any real opener (`task`/`review-request`) exists for this
  `work_item` anywhere in the full reducer history, checked FIRST and
  winning unconditionally - even over an otherwise-broken planned chain
  (a real dispatch makes the plan's own validity moot). **Terminal.**
- **unknown** - the chain cannot be resolved to one tip: a malformed
  record anywhere in it, a self-edge (`replaces` naming itself), a
  `replaces` that is missing, belongs to a different `work_item`, or
  names something that isn't a valid `planned` record, a cycle, or more
  than one tip ("competing" records). Carries a named reason. **Writes
  against an unknown item are refused** - there is no single current
  record to extend, and the evidence is kept, never silently resolved by
  falling back to an older-looking record.

**Terminal means exactly that:** `withdrawn` and `promoted` never accept
`add` or `change` again. To plan the same conceptual work again, use a
**fresh `work_item`** - the CLI refusal says so by name (§7's open
question 2 is still right not to introduce a reactivation mechanism).

### The transition table

Rows are the CLI action; columns are the `work_item`'s state **before**
the command runs.

| action | absent | active | withdrawn | promoted | unknown |
|---|---|---|---|---|---|
| `add` | **OK** - fresh record, no `replaces` | refuse: "already planned; use change" | refuse: terminal | refuse: terminal | refuse: ambiguous, cannot safely add |
| `change` | refuse: "not currently planned; use add" | **OK** - `replaces` = the resolved current id | refuse: terminal | refuse: terminal | refuse: no unique current record |
| `withdraw` | refuse: "not currently planned; use add" | **OK** - `replaces` = the resolved current id, `withdrawn: true` | refuse: already withdrawn | refuse: terminal | refuse: no unique current record |

**Structural vs. publication, kept separate.** The table above is
entirely **publication-time** (the CLI's own precondition check, §5).
**Structural replay** (§4) is a strictly smaller contract: it only ever
asks "does this one chain resolve to one tip," never "is this the first
`add` for this item" - which is why an `add`'s original record stays
perfectly valid on replay even after a `change` has replaced it; replay
has no concept of "the add command" at all, only records and the edges
between them.

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
read-side pass, `work_board.planned_state` (§2), applying the identical
structural contract `work_tags.validate_planned` applies at publish time
- never by the reducer's request/reply machinery.

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
property of the log's full history, computed by `work_board.real_openers`
and fed straight into `planned_state`'s `real_items` argument (§2), never
of what is currently *displayed* - display expiry is not a new planning
decision. **Promotion means an actual real opener in complete history -
not `work_board.reduce(...)["items"]`'s wider slug set.** That reducer
output also contains a `work_item` the audit merely *flagged evidence
against* (e.g. an ordinary `note` with a dangling `in_reply_to` and a
matching `work_item` tag, which `_audit_one` attributes via `owners()`
even though it opens nothing); counting that as promotion would let an
ordinary orphaned message permanently block `add` for a `work_item`
nothing real was ever dispatched for (fix round 1, F6). `real_openers`
only counts a `task`/`review-request` with a well-formed `request_id` -
the same admission bar `reduce()`'s own `openers` dict uses. To plan the
same conceptual work again, use a fresh `work_item`. `board plan add`
enforces this directly: it refuses, naming the existing work, when the
given `work_item` already has any real opener in history - so the lead
learns the rule at the moment they would have broken it, not by reading
this document.

**Coverage caveat:** `add`'s refusal check, and promotion generally, can
only be as complete as its view of history. `board plan` reads an
archive-aware, coverage-checked history (fix round 1, F1): both active
and compacted/archived envelopes, validated fresh against the current
roster/signing context, reusing `store._scan_messages_with_paths` +
`envelope_snapshot.validate_scanned_rows` rather than `reduce()`'s own
(non-coverage-checked) `error` field. Any invalid or unreadable envelope,
active or archived, makes completeness impossible to establish - `add`/
`change`/`withdraw` all REFUSE outright rather than asserting "no opener
exists for this `work_item`" on a possibly-incomplete view, the same
honest treatment the rest of the board already gives incomplete coverage.

---

## 6. Budget and wire shape - recorded now, built in part 2 (F3)

**Not built in this PR.** Recorded here, exactly as decided, so part 2
builds against one settled contract rather than re-deriving it:

- **The existing bounded real feed stays exactly as it is.** `bounded()`
  (`work_board_feed.py:40`)'s 100-card/256-KiB limit on `feed["items"]` is
  untouched - real cards are never trimmed to make room for planned ones.
- **Planned gets only the REMAINING headroom** inside that SAME existing
  bound, never a separate budget of its own. If even the Planned
  *summary* does not fit in whatever headroom is left, the whole Planned
  extension is **omitted** and the console shows **"Planned unavailable"**
  - a distinct state from an empty lane (zero planned items is a fact
    about the board; "unavailable" is a fact about the response budget,
    and the two must never look the same).
- **Selection is deterministic: `work_item` ascending, cap 20.** Not
  "most recent" or publication order - alphabetical by the stable key, so
  the same board state always selects the same 20 regardless of scan
  order. **Unknown items count toward the 20** - an item stuck Unknown is
  still real evidence an operator needs to see, not a free slot.
- **Diagnostics are capped by count AND bytes**, inside the same shared
  remaining headroom, not a separate allowance.
- **Overflow counts are computed AFTER byte trimming** - the number shown
  reflects what was actually left out of the response the operator
  received, never a pre-trim count that could overstate (or understate)
  what is really missing.

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

**Built and tested in part 1** (`tests/test_planned_stage.py`, plus the
existing `tests/test_store.py`/`test_work_tags.py`/`test_work_board_*.py`/
`test_recv_api.py`/`test_cli.py` suites, all still green):

1. Add, change (via `replaces`), withdraw - each produces the right
   Planned-lane state, in order; every refusal in §2's transition table.
2. Promotion: a `task` for a planned `work_item` removes it from Planned,
   permanently - checked against the full reducer history, never display.
3. Shuffled input: `work_board.planned_state` over a message list and its
   own reverse give the identical result - nothing depends on order.
4. Competing/missing/cross-item/self replaces edges, and cycles, each
   make the item Unknown with a named reason; a malformed record anywhere
   in a chain does too.
5. The isolation tests: `_audit`/`reduce()`'s output is byte-identical
   with and without a (even maliciously-shaped) planned record present;
   a planned record never refreshes `last_work_event_at`.
6. `send --kind planned` and `broadcast --kind planned` both refuse; the
   publisher gate refuses a non-lead/non-liaison sender; every published
   record is self-addressed.
7. `recv_api.records()` and `last_received_for()` both skip `planned` by
   default (still visible with explicit inspection) - no child turn, no
   default reply-anchor confusion.

**Part 2** (not yet built - the feed/console/rollout acceptance bar, kept
here for when that PR is written):

8. A busy board: 100 real cards plus 25 planned - 20 planned cards show
   (`work_item` ascending), a `5` overflow count computed after byte
   trimming, all 100 real cards untouched (§6).
9. Oversized diagnostics or an over-budget Planned summary: the whole
   Planned extension is omitted and "Planned unavailable" shows, distinct
   from a genuinely empty lane (§6).
10. Incomplete archives/coverage: `add`'s promotion-refusal check and the
    Planned lane both report Unknown, never a false "nothing real exists
    for this item" (already true in part 1's CLI refusal; re-assert once
    the feed projects it too).
11. `SnapshotService.board()`'s last-known/stale path renders `planned`
    and its overflow count identically to a fresh build.
12. An old-reader rollout: a build predating this feature rejects a
    `planned` envelope and shows `status="incomplete"`/items `unknown` -
    asserted as the documented, known limit (§7), not a surprise.

---

## 10. Open questions - resolved during the build

1. **Should `replaces` require the exact message id, or also accept a
   short form?** Resolved as recommended: `agenttalk board plan
   change|withdraw` resolves the current record and fills the exact id
   itself (§2) - a human never types a message id.
2. **Does a withdrawn item's `work_item` ever become reusable?** Resolved
   as recommended: no. `withdrawn` is terminal (§2); `work_item` identity
   is permanent once used for any planned or real record - one rule, not
   two.

## Technical details

- Supersedes revision 1-2 of this document, #266, and the parked PR #264 /
  PR #274.
- Driven by issue #279, a first design review
  (codex-agenttalk-developer-4, `tk-86567e72cad1`): RESHAPE, 7 P2 + 2 P3,
  no P1, and a confirm-read (`tk-d648679afc1a`): RESHAPE, no P1, with F2
  (the state machine) and F3 (the byte budget) needing a lead decision
  before building - both recorded in §2 and §6.
- Part 1 built on `feat/planned-stage-b1` from `origin/master`
  `c6de5ea3`. Code added/changed: `src/agenttalk/store.py`
  (`KNOWN_KINDS`, `CONTROL_KINDS`), `src/agenttalk/work_tags.py`
  (`PLANNED_FIELDS`, `validate_planned`, `PlannedRefused`, `normalize`'s
  new `kind == "planned"` branch), `src/agenttalk/work_board.py`
  (`_audit_one`'s new branch, `planned_state`, `_cyclic_replaces`),
  `src/agenttalk/work_board_feed.py` (`build`'s activity-loop exclusion),
  `src/agenttalk/cli.py` (`_cmd_board_plan`, the `board plan`
  subparsers, `cmd_send`'s refusal). Tests:
  `tests/test_planned_stage.py` (new), plus a one-line update to
  `tests/test_store.py`'s `CONTROL_KINDS` regression guard.
- Part 2 code cited for the record (not yet touched):
  `src/agenttalk/work_board_feed.py` (`bounded`),
  `src/agenttalk/envelope_snapshot.py` (`SnapshotService.board`,
  `selected_closure`), `src/agenttalk/cli.py`
  (`_recipients_behind_kind`).
