# The Planned lane, from bus records

Status: proposed. Date: 2026-10-02.

## In plain words

The work board shows work in progress, in review, and done - nothing
planned but not started. Three earlier attempts to add that (reading a
plan's prose, importing a JSON file, reading that file fresh each time)
were all parked: each invented its own file format, and each format grew
its own decoding, identity, and storage problems.

This design invents no file. The lead already tells the board about real
work by sending a `task` or `review-request` carrying a validated label
(`work_item`, a slug; `work_title`, a short name). This proposes one more
label the lead can send, carrying the same two fields, meaning "this work
item is planned." The board shows it under Planned until a real `task` or
`review-request` with the same `work_item` appears - then the board's
existing lanes take over, as today. Nothing is read but the bus log the
board already scans.

What you will notice, once built: one short command per planned item; a
Planned lane on the board. What you need to do now: review this contract.
No code is proposed yet.

---

## 1. Record format

**A new message kind, `planned`.** Reusing `note`/`message` was
considered: either works mechanically, but neither carries a validated
`work_item`/`work_title` pair, and giving one that meaning retroactively
would make every *existing* `note`/`message` ambiguous about whether it
was ever meant to tag work. A dedicated kind costs one line in
`store.KNOWN_KINDS` and gives every reader (§5) one unambiguous thing to
look for - the same reasoning that gave `task` and `rescind` their own
kinds rather than overloading `message`.

**Publisher and audience.** Only `sole_lead()`/`operator_facing()` may
publish one, checked against the live config - `cmd_task`'s gate, reused
verbatim. Broadcast (`--to all`), never one recipient: it asks nothing of
anyone, so no one party could owe it a reply. **Not** added to
`store.OPENER_KINDS` or `work_tags.OPENERS` - it opens no thread, creates
no `owed-inbound` obligation, and can never carry `supersedes`
(`work_tags.normalize` already refuses that outside `OPENERS` - enforced
by existing code).

**Meta fields**, validated through the existing `work_tags.normalize`
path (called the same way `cmd_task` already calls it):
- `work_item` - the existing slug, unchanged (`work_tags.value`, 1-64
  lowercase chars).
- `work_title` - the existing field, unchanged (1-160 chars).
- `owner`, `phase`, `starts_when` - three **new**, optional fields, each
  added to `work_tags.FIELDS` with its own cap: `owner` and `starts_when`
  1-200 chars, `phase` 1-64 chars. Free display text, not a closed
  vocabulary - a plan phase is not a dispatch stage (next point).
- `withdrawn` - a new, optional boolean field (§2).

**`planned` is never a `stage`.** `work_tags.STAGES` stays exactly
`{design, build, read, fix, delta, sweep}` - a dispatchable value a
`task` can carry. A planned record has no `stage` at all; its `phase`
field is a different, free-text concept, so the two can never be
confused by a reader that (correctly) only trusts `stage` for real work.

**Command surface:** `agenttalk board plan add|change|withdraw`, a new
`plan` action nested under the existing `board` subcommand (today's only
`board_cmd` is `verify-merges`; `import-plan`/`retire-plan` from the
parked branch were never ported to `master` - this is a fresh, small
addition, not a restoration).

---

## 2. Lifecycle

- **Add:** `board plan add --work-item X --work-title T [--owner --phase
  --starts-when]` publishes one `planned` record.
- **Change:** `board plan add` again for the same `work_item` - a later
  record replaces the earlier one. "Later" means its position in the bus
  log (the append-only order every reducer here already trusts; never a
  timestamp - `work_board.py`: "Message IDs and timestamps never order
  evidence").
- **Withdraw:** `board plan withdraw X` publishes a `planned` record for
  `X` with `withdrawn: true`, no other fields required. Not `supersedes`:
  that means "this dispatch replaces that one, same work" - stronger than
  "stop planning this." A withdrawal is just the latest record for `X`,
  reusing "later replaces earlier," not a second mechanism.
- **Promotion:** a `task`/`review-request` with the same `work_item`
  anywhere in the log moves it out of Planned - "planned minus every
  `work_item` the board's reducer already found a real opener for" (§7).
- **Re-planned after done:** if `X`'s record is never withdrawn and its
  real work ages off the board (the existing 7-day Done window), `X`
  reappears in Planned - the old record is still the latest for `X`. Not
  a bug; the lead's habit should be "withdraw when planning the *next*
  thing for `X`," not only when dispatching it.
- **Order in the lane:** most-recently-planned first (bus position),
  `work_item` as tiebreak - matching every other lane's own convention.

---

## 3. Budget

- **Cap on cards shown:** at most 20 Planned cards. Past that, one
  overflow card, "+N more planned items" - it is never counted against,
  and never displaces, any in-progress card; Planned is its own lane,
  never sharing a slot budget with Building/Review/Fix/Ready/Done.
- **Field caps are refused at publish**, not truncated and not stored
  short: `work_tags.value` already raises on an oversized `work_item` or
  `work_title`; §1's three new fields get the same treatment, so an
  invalid record never reaches the log (§4).
- **Warning-byte budget:** any warning this feature adds (§4's malformed-
  record case) joins the board's existing bounded warning list
  (`errors` in `work_board_feed.build`'s return value) - same cap, not a
  new one.
- **Cost: the bus scan the board already does.** `work_board_feed.build`
  already materializes every message into one `messages` list before
  doing anything else (`work_board_feed.py:68`). Finding `planned`
  records is one more pass over that same, already-in-memory list - no
  new file, no new directory, no second scan.

---

## 4. Failure policy

- **Refused at publish:** an oversized or wrong-typed field, a
  `work_item` that collides with `supersedes`-style replacement syntax,
  or `withdrawn` alongside any other field, all refuse before the record
  is written - the same shape as every other `work_tags.normalize`
  refusal today.
- **Malformed record already in the log** (hand-edited, or written by an
  older build missing a field this design later added): the board's
  per-message isolation already does this for every kind
  (`envelope_snapshot.validate_scanned_rows`, `work_board.py`'s own
  per-item `Unknown` isolation) - one bounded warning, zero rows lost
  from any other item.
- **Older readers, and why `--force` is genuinely needed here.** An older
  build's `KNOWN_KINDS` lacks `planned`; its envelope scan rejects it as
  invalid rather than crashing (`envelope_snapshot.py:48-58`) - but that
  reject is not free: `selected_closure` turns any nonzero
  `invalid_count` into `status="incomplete"` (`:114`), and
  `work_board_feed.build` then marks **every** item `column="unknown"` on
  incomplete coverage (`work_board_feed.py:133-136`). Not new to
  `planned` - already true of `task`/`rescind`/any kind ever added - but
  one unrecognized record can blank an older reader's *whole* board, not
  just hide the new lane. `cmd_task` checks only its one recipient's
  version, because only that recipient needs to understand `task`;
  `planned` has no single recipient, so publishing it must check the
  **whole live roster's** version against a new
  `KIND_SUPPORT_FLOOR["planned"]` entry - same mechanism, wider audience.
  Behind: refuse, naming who, unless `--force`.

---

## 5. Reader audit

| Reader | Effect |
|---|---|
| `work_board.reduce`/`_audit` | **Unaffected.** Only scans `m.kind in work_tags.OPENERS` (`task`, `review-request`); `planned` is deliberately excluded from `OPENERS` (§1). |
| `work_board_feed.build` | **Changed, additively.** One new pass over the already-materialized `messages` list builds the Planned set (§7); every existing line is untouched. |
| `threads.py` / status WARNs | **Unaffected.** Thread derivation keys off `store.OPENER_KINDS`; `planned` is not in it, so it opens no thread and trips no owed-inbound warning. |
| Checkpoint owed counts | **Unaffected.** Owed counts are thread states rolled up; no thread, no count. |
| `attention.py` | **Unaffected.** No attention source reads generic kind/meta beyond `needs_operator`'s own `meta.attention` block, which a `planned` record never carries. |
| Console feeds (`/api/state`, `/api/messages`, `/api/threads`) | **Unaffected.** A `planned` record displays like any other ordinary, kind-labeled message - the same way `rescind`/`release` already do. |
| Dead letters | **Unaffected.** Same broadcast/send path as any kind; creates no reply obligation, so there is nothing for a recipient to get stuck failing to answer. |
| `work_tags.normalize` | **Reused, not changed** - it already validates every `FIELDS` key present in `meta`, for any `kind` (`work_tags.py:230-234`); the three new fields just join `FIELDS` (§1). |
| Gates (`gates.check_board`/`check_gates`) | **Unaffected.** Gate checks run over real openers found via `work_tags.OPENERS`; `planned` is never in that set. |

---

## 6. Acceptance cases

1. Add a planned record; it appears in the Planned lane with its title/owner/phase/starts-when.
2. Change: a second `add` for the same `work_item` replaces the first; only the latest shows.
3. Withdraw: the item leaves Planned, with no card left behind.
4. Promotion: dispatching a `task` for a planned `work_item` moves it to its real lane and out of Planned, same bus build.
5. Re-plan after done: a withdrawn-then-added record reappears correctly; an un-withdrawn one reappears once its done item ages off the board (§2), and this is asserted as expected, not a regression.
6. The cap: 21+ planned items show 20 cards plus one "+N more" card; no in-progress card is displaced.
7. Every refusal in §4's "refused at publish" list is exercised, each leaving the log unchanged.
8. A malformed `planned` record hand-inserted into the log: one bounded warning, every other item unaffected.
9. An older-build seat (or the board server itself pinned old): confirm `board plan add` refuses naming who is behind, that `--force` sends anyway, and that the resulting old-reader board shows `status="incomplete"` / all-`unknown` exactly as §4 predicts - not silently wrong.

---

## 7. Reuse

**Port:** the PLANNED label and planned-card rendering in `console2.js`,
and the *shape* of the dispatched-vs-planned merge in `planned_cards`
(planned set minus dispatched set, cards for the difference) - its
*inputs* change completely: no stored section, no file; both sets come
from the one `messages` list `work_board_feed.build` already holds (§3),
computed directly there, not a separate `work_board_facts` reader.

**Do not port:** anything that reads a plan file - `_classify_line`/
`_parse_plan` (the Markdown parser), every JSON-sidecar reader/validator
from #274's revisions 1-3, `_write_section`'s merge extension,
`_planned_build`, `retire_plan`. None have anything left to do.

---

## 8. Open questions

1. **Is 20 the right cap (§3)?** Chosen to match a human's glance at a
   board, not measured against a real roster's planning volume.
   Recommendation: ship with 20, revisit once a real project's lead has
   used it for a sprint.
2. **Should the whole-roster version-floor check (§4) block, or only
   warn?** Recommendation: block by default with `--force` to override,
   matching `cmd_task` exactly - a board-wide effect deserves at least
   the same friction as a single-recipient one.
3. **Does `phase` need any validation beyond a length cap** (for example,
   rejecting blank/whitespace-only text, as the parked attempts' field
   rules did)? Recommendation: yes, reuse the same non-blank check
   `work_tags.value` already applies to `work_title`, rather than writing
   a new one.

## Technical details

- Supersedes #266 and the parked `feat/wb-planned-lane` branch (PR #264)
  and PR #274 (revisions 1-3, parked - see its park note for what each
  revision learned).
- Driven by issue #279.
- Code cited: `src/agenttalk/store.py` (`KNOWN_KINDS`, `KIND_SUPPORT_FLOOR`,
  `OPENER_KINDS`), `src/agenttalk/work_tags.py` (`FIELDS`, `STAGES`,
  `OPENERS`, `value`, `normalize`), `src/agenttalk/work_board.py`
  (`reduce`, `_reduce`), `src/agenttalk/work_board_feed.py` (`build`),
  `src/agenttalk/envelope_snapshot.py` (`validate_scanned_rows`,
  `selected_closure`), `src/agenttalk/cli.py` (`cmd_task`,
  `_recipients_behind_kind`) - all read at `origin/master`,
  `4e8cf71c`.
