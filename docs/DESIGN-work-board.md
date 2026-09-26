# A work board that moves itself

Status: proposed design for [#207](https://github.com/zoolok17/agenttalk/issues/207), 2026-09-26.
Audience: implementers and cold readers deciding whether this contract is buildable.
Mode: explanation with proposed contracts; none of the new commands or feeds below exists yet.

The operator decided that the board is read-only, derived from agents' work, and comes before
[plan review #206](https://github.com/zoolok17/agenttalk/issues/206). A card moves because evidence
arrived, not because someone dragged it. The board describes work; it neither authorizes a merge
nor replaces the bus, gates, lead, or an existing project planner.

## 1. What exists, and what is missing

Code inspected: master `01ddb455`, and console v2 `5468cf9` without modifying its branch.
Integrate the frontend after M4 is accepted; recheck its actual head then. The step record is
`docs/STEP-CONSOLE-V2-PITCH.md` on `feat/console-v2-pitch`.

| Existing surface | Consequence for this design |
|---|---|
| `store.py:Message.to_dict/from_raw`, `valid_messages`; envelope is `id, ts, from, to, kind, subject, body, meta` | Use validated envelopes and correlation, never keyword searches in bodies. Preserve signature and active/retired-roster validation. |
| `cli.py:cmd_task` checks lead/liaison and accepts free-form `--meta` | Today's convention is readable, but misspelled slugs/stages are not prevented. |
| `threads.py:_classify_event` | Task `status=accepted` remains open; `done/declined` are terminal. A terminal reply is not necessarily success. Reuse this logic. |
| `cli.py:cmd_reply`, `reply_transport` | Correlation is echoed; other metadata is not automatically echoed. Inherit item/stage from the opener, not the reply. |
| `web.py:_validated_for_state`, `build_threads_index` | Existing feeds scan the message directory. `/api/threads` paginates closed output, not scan work. Do not add another full scan per poll. |
| `/api/state.recent` is capped at 25; `/api/messages` is not a board index | Neither proves complete work history. `/api/thread/<rid>` is the on-demand transcript. |
| `build_attention` consumes canonical attention sources/dispositions but omits their general `source_refs` in ordinary wire rows | Add bounded correlation references; never assign a warning to an item by title or by seat alone. |
| v2 `buildTeamView` combines attention with locally derived stuck incidents, then applies Later/Wait | Extract that calculation for reuse; the board must not invent a second needs-you detector. |
| `health.py`, `wrapper/health.py` carry `request_id` and `msg_id` when known | A fresh exact match can describe current execution. A generic busy seat, stale health or an idle seat cannot advance a work item. |
| `gates.py` has typed status, scope, revision and evidence | Reuse gate validation; a prose “tests passed” or a green gate for a different revision cannot make Ready. |
| `ovh_gateway.py` joins `child_turns(agent,message_id,request_id)` to attempts | Attribution exists, but its `exposure_micro_eur` mixes settled charges and reservations. Do not label it “spent.” |

## 2. Identity and declarations, not editable workflow state

Choose validated first-class flags and a small local registry. Meta-only is sufficient to show
dispatched work, but insufficient to prove grouping, expected reviews, repository identity or completion.
The registry contains identity, scope and evidence requirements, **never a writable column/status**.
The console has no create/edit/reorder/move API. The lead records these facts as part of normal dispatch.

Identity is `(project_id, work_item)`; `project_id` is the bus root identity, not its filesystem path.
`work_item` is an immutable ASCII slug matching `[a-z0-9][a-z0-9-]{0,63}`. Titles are separate bounded
text (160 characters), not identifiers. Different roots never merge equal slugs. One item is one
reviewable deliverable with one repository and target branch; multi-repository work uses separate
items with optional related-item links. Every task belongs to at most one item and one cycle.

Proposed `agenttalk work-item register --file <document>` accepts a closed version-1 declaration:
`work_item, title, repo_alias, branch, target_ref, cycle, requirements` (plus `schema_version=1`).
`repo_alias`, `branch` and `target_ref` may all be null for a not-yet-bound item; partial bindings fail.
`cycle` starts at 1. Requirements contain `required_review_requests[]`, `required_gate_names[]`,
`no_gates_reason` (null unless the gate list is explicitly empty), `independence` (`cross_vendor`
by default, or explicitly `different_seat`), and `review_set_sealed` (false until dispatch is complete).
Review IDs may be added while unsealed;
sealing freezes the set, which must contain at least one independent reviewer before Ready.
A small append-only declaration log under the root records actor, timestamp, previous revision and
content hash; updates use compare-and-swap. The materialized registry is rebuildable. No mutable task
list duplicates the bus. Referenced IDs must resolve to tasks/reviews for this item and cycle.
Registration and amendments require the same current lead/liaison authority as task dispatch;
repository alias approval remains operator configuration. Read operations require no new privilege.

Proposed flags on `task` and `review-request`: `--work-item`, `--stage`, `--work-cycle`, `--work-round`,
and optional `--work-head` (full commit OID). They serialize into `meta.work_item`, `stage`, `work_cycle`,
`work_round`, `work_head`. Stages are exactly `design|build|read|fix|delta|sweep`; cycle/round are positive
integers (wire decimal strings, normalized internally). Unknown registered item, bad enum, incompatible
duplicate `--meta`, or wrong root is exit 2 before publication. Untagged legacy commands still work.
`reply --verdict GO|FIX|HOLD|done` is validated sugar for `meta.verdict`; it does not change bus kinds or
terminal-status rules. A task completion needs `status=done`; `verdict` does not close a thread by itself.
Apply the same validator at the shared publication boundary and draft delivery, not just CLI parsing.

Registry updates also support candidate binding `(cycle, head_oid)`, sealing review requirements,
explicit request binding, and starting cycle N+1. They cannot set Ready/Done or erase historical
verdicts. Changing a candidate OID invalidates earlier revision-bound approvals/checks. A new cycle
retains the old cycle's completion evidence. Reducing sealed requirements requires an audited new
cycle; it cannot silently turn a HOLD into Ready. This is evidence bookkeeping, not a second planner.
Use proposed `work-item amend --file <document> --expected-revision <hash>` for a new declaration
revision and `work-item bind --file <document>` for explicit `{work_item, cycle, opener_ids}` corrections.
Review replacement records are `{old_request_id, new_request_id, candidate_oid}` in the amendment:
same item/cycle, no cycles in the replacement graph, and one replacement per original obligation.
They preserve the sealed requirement rather than remove it; until the replacement is GO the
requirement remains unsatisfied. A candidate change requires replacement reviews for every required
reviewer, not just the one who found a problem. Old GO replies cannot bless unseen changes.

### Existing convention, untagged history and mistakes

1. A valid existing `meta.work_item` groups openers with that exact slug. It creates a provisional
   identity, not an implicit registered repository or approval policy. Display “unregistered.”
   The first opener's subject supplies a provisional title. A declaration later preserves the slug.
2. Untagged openers get isolated `legacy:<opener-id>` cards in an **Unclassified** section. Resolve
   their request threads normally, but do not guess common work from similar subjects or authors.
   Default history is 30 days; an explicit legacy-history query can page older records.
3. Replies inherit grouping through validated `request_id`/`in_reply_to` and the expected participants
   and kind. Use the existing thread reducer, including rescind and broadcast semantics. Duplicate or
   ambiguous opener IDs/correlations produce `unknown`, not a best-effort join. Generic messages do
   not close tasks. Broadcast copies are separate obligations; an item cannot lose an outstanding copy.
4. A typo in a registered slug fails at dispatch. Legacy typos remain separate until a lead/liaison
   records an audited binding of **explicit opener IDs** to the correct item/cycle. Do not fuzzy-merge
   slugs, rewrite signed messages, or infer aliases from proximity. Conflicting bindings are unknown.
5. A terminal task reply without a verdict closes its transport obligation but has outcome unknown.
   `status=declined` is never success. For legacy `review-result`, approved/rejected/needs-info may map
   to GO/FIX/HOLD with provenance; contradictory explicit verdict/status is invalid board evidence.
   Approval still needs the correct candidate and requirements. Do not manufacture these retroactively.
6. A seat-side reply draft is not a bus event. Read only the wrapper's published validated reply.
   Refused/unpublished drafts may yield an existing attention warning, never a success transition.
   No board scan opens draft contents. Missing replies remain outstanding even if health goes idle.

## 3. Derivation and precedence

Derive per cycle from validated task obligations, their terminal replies, candidate evidence and
canonical attention references. Order events by bus message ID, not wall-clock arrival at the cache.
Retain all required parallel reviews: a later GO from one reviewer cannot erase another's FIX/HOLD.
A FIX remains unresolved until its request is explicitly superseded by a linked delta review for a
new candidate, which itself must receive GO. Merely dispatching fixes does not resolve findings.
No missing review/check is interpreted as green. Completed rounds are retained as history.

“Building” means assigned/accepted build activity, **not a claim that a process is currently running**.
The task chip separately says `dispatched`, `accepted`, `running (observed)`, `reply received`, or
`unknown`. Running requires fresh health with the exact task request/message and expected seat.
Without such evidence, omit the live pulse. Round is explicit `work_round`; legacy round is null,
never a count inferred from how many messages the agents sent. Design tasks appear under Building
with a Design badge; read/delta/sweep appear under Independent review with the specific stage badge.

First matching row wins for the **current cycle**, after input validation. Keep every contributing
request/evidence ID on the result so the detail view explains the placement.

| Priority | Proven condition | Column / reason |
|---|---|---|
| 1 | Complete canonical attention incident explicitly linked to this item, unresolved and requiring operator attention | Needs you; preserve underlying workflow column. HOLD alone is not proof that a human is needed. |
| 2 | Item identity/history/correlation is contradictory, index incomplete, or required workflow source failed | Unknown / specific missing evidence. Show in Unclassified; preserve last-known column separately. Never silently put it in Queued. |
| 3 | Bound candidate is proved integrated into configured target, current cycle has no later candidate or outstanding task, and required source reads are complete | Done / merged locally. Record whether review/check evidence was complete; merging does not retrospectively prove review. |
| 4 | Required review returned FIX, or an outstanding fix task exists | Fix round / changes requested or fix dispatched. An active delta review for its replacement candidate shows Independent review with prior FIX retained as history. |
| 5 | Outstanding build/design task exists | Building / assigned stage; parallel review is a secondary badge. |
| 6 | Outstanding read/delta/sweep exists and independence is established | Independent review / awaiting required replies. If independence is unproved, Unknown / review independence unknown. |
| 7 | Candidate and sealed required review set exist; all required independent verdicts are GO for that candidate; required gates are validated green for that revision/scope; all work obligations terminal-success; no unresolved FIX/HOLD or operator incident | Ready / reviewed and checked, awaiting integration. No implied merge authorization. |
| 8 | Declaration exists with no dispatched tasks in this cycle | Queued / not dispatched. |
| 9 | Otherwise, including completed work missing verdict/checks, unresolved non-operator HOLD, or untagged stage | Unknown / review held, verdict missing, checks missing, or stage unknown. |

The replacement-delta exception in row 4 requires an explicit supersession link and candidate binding;
it is not triggered by any arbitrary read. Multiple active fixes keep row 4 until all their replacement
reviews are dispatched; any remaining FIX for the current candidate keeps it there. HOLD blocks Ready
even alongside GO. A rescinded task is cancelled, not successful; cancellation is visible and does not
make an otherwise unproved deliverable Done. A new cycle reopens the item, without altering its history.
Merge, PR and cost observations are separately qualified optional sources: failure of those reads cannot
erase a proven workflow stage. Unknown merge prevents Done but does not prevent Ready. Unknown gates
or review coverage prevent Ready; unknown health removes Running, not the recorded dispatch stage.

Independence means reviewer seat differs from every builder for that candidate, and the recorded runtime
vendor differs from its primary builder when the declared review policy requires cross-vendor review.
Snapshot reviewer/vendor assignment at dispatch from configured runtime facts; do not infer vendor from
an arbitrary seat-name prefix. Old dispatches lacking that evidence cannot claim verified independence.
All declarations and outcomes remain within the existing trusted-local-store model; this is not a new
security boundary against someone who can rewrite that store.

## 4. Ready, Done, repositories and PRs

Repository bindings are **operator-configured local aliases** under the bus root, each with an approved
absolute local checkout and target ref. Multiple bus items may refer to another project's local clone.
The web request supplies only root/item IDs, never a filesystem path, executable or Git arguments.
Check canonical paths against that registry; reject traversal, escaping symlinks, option-like refs and
non-repositories. Do not discover repositories by scanning disks, message bodies, issue URLs or cwd.
An item branch is informational until its candidate is pinned to a full OID. A branch moving invalidates
its old Ready display until the lead binds and reviews the new candidate; force pushes are not merges.

A bounded local observer records repo alias, branch tip, candidate, target ref/tip, observation time and
ancestry result using shell-free, timed Git reads with lazy network fetch disabled. Missing objects,
shallow history, disappearing worktrees, changed alias mappings and timeout all mean unknown. The web
server never runs `fetch`, `gh`, remote Git, hooks, or credentials helpers. Limit each Git probe to 2 s
and one observer probe at a time; requests consume its cached result only. Recheck visible repositories
at most every 10 s; label all evidence “local refs as of …,” never “GitHub live.”

Fast-forward/ordinary merge proof: the pinned candidate OID is an ancestor of the configured local
target tip. Source-branch deletion, a closed PR, task `verdict=done`, or a lead's prose are not proof.
Missing local refs leave Ready (if other conditions hold), with merge state unknown. Done can be shown
without a PR. Existing green gates are reused only when their revision and scope match the item; a
global GO whose evidence is not bound to this candidate is insufficient. An empty declared gate list
must be explicit with rationale; missing requirements are not an empty successful set.

For squash/rebase, v1 does not infer patch equivalence. An optional **lead-side** evidence import can
record a trusted PR snapshot collected outside the server: repo alias, PR number, source head OID,
state `open|closed|merged`, resulting merge OID, observed time and source receipt. Done additionally
requires that merge OID is present in the configured local target ancestry and the receipt pins the
reviewed source head. Closed-unmerged is not Done. If no such receipt exists, show “integration unproved.”
PR badge always includes snapshot age. This optional adapter is separately sized; the first demo uses
ordinary local ancestry, so board delivery never waits for GitHub access.

## 5. One needs-you source, and the card

Extract `deriveNeeds(input)` from the v2 model: attention items, lead-chat pending decisions and the
existing stuck detector enter once, with one stable incident ID and source freshness. Both stream and
board use the same output. Add sanitized `source_refs` (request/message/gate IDs, not bodies or paths)
to attention entries so task-to-item linking is exact. A stuck incident maps only through fresh exact
health correlation; if association is ambiguous, keep it in team attention, not every card for the seat.
Do not infer a work item from the lead-chat text. Deduplicate an escalation exposed through two feeds
using its canonical request ID. The common model owns all precedence involving Needs you.

Later/Wait only hide or defer presentation locally. They never resolve an incident or move work back
to Building. Both views say “deferred” for the same incident; board remains in Needs you, while the
stream lists it in its deferred group. A new turn incident survives an old deferral. A stale attention
read retains the last-known incident with its timestamp; an error/empty payload cannot clear it or
make Ready. Unmapped team attention remains visible above the board, not lost in grouping.

Cards contain title, stage/column reason, seat names and verified vendor badges (unknown badge when
unavailable), explicit round or “round unknown,” outstanding/total request counts, and two ages:
time since first dispatch and time since last meaningful workflow event. Heartbeat/poll time does not
reset either. Focusable evidence links open bounded detail; titles and excerpts use textContent.
Open findings are `{status:"unavailable", count:null}` until #206 supplies stable finding IDs and a
resolution reducer. A FIX is “changes requested,” never a guessed count from Markdown bullets.
The future join is `(project_id, work_item, cycle, candidate_oid, finding_id)`; stale-revision findings
stay in history. A plan artifact link uses a validated document reference from #206 when available;
until then the click opens today's task/evidence detail, not a disabled promise of an editor.

Cost is null by default. A read-only gateway adapter may sum uniquely attributed child attempts by
`(gateway_alias, ledger_generation, attempt_id)` and correlate child `(agent,message_id,request_id)`
to exact tasks. Show settled micro-EUR and reserved/uncertain exposure separately, as-of time, and
coverage `partial|complete` across **known metered turns**, not the whole team's true cost. No sum of
both per-attempt and per-turn aggregates. The current exposure aggregate is not settled spend.
Missing/remote ledger, overlapping attribution or missing generation yields unknown/partial, not zero.
Subscription usage percentages are not EUR; do not estimate salaries, vendor costs or “money saved.”
Keep this optional adapter after the basic board; the demo may honestly say “cost not reported.”

## 6. Bounded derivation and feed contract

Server: pure Python reducer over normalized validated envelopes and declarations, plus local evidence
adapters and a derived cache. Browser: pure Node-testable view model for formatting, shared attention,
freshness, filters and final Needs-you overlay; renderer only reconciles DOM. Do not implement task
closure twice in Python and JS. Retain the current thread reducer's semantics through an indexed adapter.

Do not poll `/api/messages`, scan all history in JavaScript, or call the current full-scan state builder
from the new handler. One per-root background cache builder reads files in bounded slices, maintaining
an in-memory envelope index by message ID, request ID and item. Cache is disposable; JSON bus files
and audited declarations remain authoritative. No cache error may prevent message publication.

Initial implementation limits: 50 cards/page, maximum 100; detail 50 tasks/page and 50 evidence refs/page;
poll selected board every 5 s, at most one board request in flight, 5 s timeout including JSON decode.
Page responses capped at 256 KiB; detail bodies use the existing transcript route on demand. Browser
stops background board polling when its tab is hidden and refreshes on return. Per-root cache cap:
50,000 envelopes / 64 MiB (whichever first), 2,000 item summaries; exceeding it is `capacity_exceeded`
with last-known data, never silent truncation presented as complete. No automatic archival of open work.

The builder keeps an `os.scandir` iterator (no sorted full-directory list each poll) and processes at
most 250 entries, 2 MiB of file content, or 25 ms per cooperative slice, whichever first. A single file
above the existing accepted message-size limit is handled as an explicit coverage problem, not truncated
into a valid envelope. Reuse Store schema/filename/roster/signature checks; body is discarded after
validation. A read can exceed the time target on slow storage: it runs outside the HTTP thread and
cannot refresh the snapshot's validity. Only one root's slice runs at once; round-robin roots.

Repeat directory sweeps to discover new, edited, deleted and quarantined files, including IDs older than
the high-water mark. A monotonically increasing message ID alone is **not** a correctness checkpoint.
Cache unchanged parsed envelopes by file fingerprint; changed files are revalidated. Config/signing/
registry changes invalidate dependent projections. Publish a new immutable generation only after a
complete sweep and dependency reads; retain scan-start/scan-end times. It is an observation interval,
not an atomic bus transaction. Mid-sweep changes to already visited paths appear next sweep; this is
bounded eventual consistency, visibly “as of,” never authority to merge. Unknown/partial bootstrap
cannot emit Ready/Done as current. After restart all cached claims stay unavailable until rebuilt.
Workflow freshness requires scan-start age <=15 s and successful workflow dependency reads; Git/PR/cost
carry their own observation times and errors and cannot refresh that clock. If the store cannot
be swept within that budget, retain stale data and report index lag. Do not claim live by stamping a
fresh HTTP response. A later optimization may use a writer journal, but is not required for this slice.

HTTP does O(page-size) cache work and **zero message-file/Git/network reads**. Sort keys are cached
`(last_work_event_id, item_id)` descending with deterministic ties. Page cursor includes generation,
filter hash and last key; retain two generations for 30 s, otherwise return `409 cursor_expired` and
restart explicitly. Never splice pages from different generations. Open items of every age stay in
the active query; Done/legacy history defaults to 30 days with an explicit dated history filter.

Proposed GET `/api/work-board?root=<project_id>&view=active|history&limit=50&cursor=...`:

```json
{
  "schema_version": 1, "target_root_project_id": "project-id",
  "generation": "opaque", "generated_at": "UTC timestamp",
  "coverage": {"status": "complete", "scan_started_at": "UTC timestamp", "as_of": "UTC timestamp", "valid_until": "UTC timestamp"},
  "items": [{
    "id": "project-id/work-slug", "work_item": "work-slug", "title": "Bounded title",
    "cycle": 1, "round": 1, "workflow_column": "independent_review",
    "reason": "review_pending", "as_of": "UTC timestamp", "last_known_column": null,
    "candidate": {"repo_alias": "service", "branch": "feature", "head_oid": "full OID", "target_ref": "refs/heads/main"},
    "tasks": {"open": 2, "total": 3}, "seats": [{"name": "reviewer", "vendor": "unknown"}],
    "attention_refs": [], "evidence_refs": [{"kind": "message", "id": "validated message id"}],
    "first_dispatch_at": "UTC timestamp", "last_work_event_at": "UTC timestamp",
    "findings": {"status": "unavailable", "count": null}, "cost": null,
    "merge": {"status": "unknown", "observed_at": null}, "issues": []
  }],
  "next_cursor": null, "errors": []
}
```

Illustrative shape, not executable input. Closed enums: workflow column is
`queued|building|independent_review|fix_round|ready|done|unknown`; final view adds `needs_you`.
Coverage is `complete|building|stale|unavailable|capacity_exceeded`; merge is
`integrated|not_integrated|unknown`. References use `message|request|gate|declaration|git|receipt`.
Reasons are bounded machine codes for the precedence rows, not arbitrary exception text. Optional
unknown values are null, not zero/empty success. All shown keys are required; candidate may be null.
`issues` lists bounded `{code, evidence_refs}` anomalies; invalid records cannot enter successful counts.
An errors-as-data response is not a fresh empty board. The client preserves last good data and greys it.
Wrong/repeated/unknown root selectors fail closed like existing selected-root feeds. New detail route
GET `/api/work-board/<slug>` uses the same root selection and cursor discipline, never raw path input.

Performance acceptance (measure during build, not claimed here): synthetic 10,000 real-shape messages,
500 items, 20 seats; warm handler p95 <50 ms, builder slices target <=25 ms, cache RSS within cap,
no extra full Store scan per board request. At 50,000 envelopes test explicit lag/cap behavior rather
than promise live performance. Count file reads and Git invocations as well as timings. Existing v2
feeds can still be costly; this proposal bounds the board's incremental cost, not all server activity.

## 7. UI and future plan review

Add a read-only Board tab within `/v2`; retain Conversation and the M4 rail. Board is the demo landing
tab via a validated local URL selection, without changing `/` or the default for existing users.
Seven named columns plus a visible Unclassified tray; horizontal scrolling at narrow widths, no drag
handles or reorder affordance. Key cards by stable root/item ID through DOM reconcile. When a card
changes column, preserve focused element and scroll intent; announce the move via a polite live region,
do not auto-scroll someone reading a detail. Render meaningful “no matching work,” loading and stale
states separately. Counts include explicit deferred needs-you incidents, not only visible stream cards.

All requests from the board are GET, same loopback and CSP policy as `/dashboard`. Use vanilla JS,
textContent and CSS classes; no inline handlers or style attributes. Links are constructed from validated
local IDs; bus-supplied URLs are not assigned as href. No enabled Approve/Send/Move control. #206 can
later open its own separately gated write-capable plan view from a validated artifact reference; that
does not make this board writable or allow the board to issue a GO.

## 8. Lead skill addition (proposed universal text)

Apply identical semantics to both shipped lead skills; keep shell examples platform-specific.

> Before dispatch, choose one stable work-item slug for the deliverable and register its title and
> repository alias if known. Reuse that identity through design, build, review, fix, delta and sweep.
> Record the cycle and round; do not encode changing stages in the slug. With older runtimes, pass
> `--meta work_item=<slug> --meta stage=<stage>` and label missing registry evidence honestly.
>
> Use validated work-item flags when available. Pin the candidate revision before review, record the
> required independent reviewers and checks, and seal the review set once dispatch is complete.
> Ask for a typed reply with terminal status and verdict. GO is a review result for a revision;
> done completes the assigned task. Neither says the deliverable was merged.
>
> Correlate replies to their original requests. Link fixes and replacement delta reviews explicitly;
> never bury a prior FIX/HOLD under a newer favorable reply. Escalate only decisions requiring a human,
> attaching the work-item/request reference. Report missing evidence rather than guessing a column.
>
> Correct a mistaken binding with an audited explicit request-ID correction. Do not edit old messages
> or move cards. Keep repository aliases local and approved; record merge evidence from the configured
> target, and publish any remote PR snapshot outside the console server. Do not grant new permissions
> or perform a merge merely because the board says Ready. Existing gates and project planners govern.

## 9. Build slices and acceptance

Each slice targets **about 700 changed lines including tests/docs**, cold-readable independently.
Split a slice before implementation if its plan exceeds that budget; no milestone may defer its error
paths to a later “hardening” step. Backend slices suit a strong Python seat; UI slices suit frontend-dev;
money attribution needs the gateway owner. Every slice gets a different-vendor cold reader.

| Slice | Deliverable / owner | Required targeted evidence |
|---|---|---|
| B1 | Identity declarations, registry validation and audited request bindings / Python | `board_identity`: two roots/same slug, typo, conflict, CAS race, invalid ref, legacy adoption, no mutable status. |
| B2 | Validated dispatch/reply flags and both lead skill updates / Python | `board_transport`: existing untagged compatibility, invalid enums, wrong item, correlation inheritance, terminal status vs verdict, draft publication parity, retired identities. |
| B3 | Pure workflow reducer / strongest reasoning Python seat | `board_precedence`: every table row; shuffled input; two reviewers GO+FIX; HOLD+GO; rescind/decline; accepted vs done; missing verdict; candidate change; superseded delta; no required-review set; exact health correlation. |
| B4 | Bounded envelope cache and rebuild lifecycle / Python with IO experience | `board_index`: >25 messages, old open item, late low-ID file, edits/quarantine, invalid signatures, partial rebuild, restart, caps, cache freshness and actual measured work bounds. |
| B5 | Local Git/gate evidence adapter / Python | `board_completion`: merge/FF, branch moves, shallow/missing repo, separate repo alias, no PR, stale gates, wrong SHA, timeout, no network/lazy fetch, no execution from hostile refs. |
| B6 | GET list/detail feed and pagination / Python | `board_api`: root isolation, expired cursors, stable pages, body/path redaction, read-only routes, errors-as-data, no per-request scan; benchmark specified above. |
| B7 | Shared needs-you model and safe correlation references / frontend + bounded backend field addition | `board_attention`: same incident in stream/board, unknown mapping, stale feed, Later/Wait, new turn, duplicate escalation, no false human escalation from HOLD. |
| B8 | Board view model/rendering, detail and demo fixture / frontend-dev after M4 | Pure Node real-envelope fixtures plus real-browser `board_journey`: moving card retains focus/scroll, root changes, stale/empty/error states, keyboard, 1024/1240 widths, CSP/textContent, GET-only, no dead controls. |
| B9 (optional after demo) | Local read-only gateway cost adapter / gateway owner | `board_cost`: actual vs reserve, retries, ledger generation, dedup, missing/remote ledger, partial attribution, no account opening balance as task spend. |

B1-B6 can proceed without #206. B7-B8 integrate the accepted M4 rather than editing its active branch.
PR snapshot import is a separate follow-up, estimated and cold-read before dispatch. Findings rendering
waits for #206's stable contract; nullable fields already give it an insertion point. No new task kinds,
network service, framework, dependency, writable board or full-text history search is required.

Node fixtures must be adaptations of serialized `Message.to_dict` and actual feed outputs, including
`meta.request_id`, `in_reply_to`, `status` and `verdict`; do not test only an invented flat `task.state`.
Pure Python tests own task-state truth; Node tests prove normalization, shared attention and presentation.
Browser tests must exercise the mounted app and polling, not just detached mock DOM elements. Use a
temporary isolated bus and Git repositories; no fixture writes to the real team's store.

## 10. Demonstration and decisions

1. Open `/v2` on Board with a visibly labelled demo root. Show one queued registered deliverable and
   “derived from agent activity”; no dragging controls. Keep evidence drawer accessible throughout.
2. Lead dispatches a real build task in the isolated demo bus. Card moves to Building, labelled
   dispatched; exact fresh wrapper health can add Running. Open its request to show the source.
3. Builder publishes a typed completion with a candidate SHA. It does **not** jump to Done. Lead
   dispatches/seals independent reviews; card moves to Independent review, showing vendor badges.
4. One reviewer replies FIX, another GO. Card moves to Fix round and retains both evidence links.
   A linked replacement candidate and delta review bring it back to Independent review.
5. Lead publishes a real linked operator escalation. Card moves to Needs you and the Conversation
   stream shows the same incident. Later marks it deferred in both places; it does not resolve work.
   A typed operator answer through the existing authorized path removes that blocker.
6. Required GO replies and revision-bound green check evidence make Ready. It says awaiting merge.
7. Lead performs the already-authorized local merge outside the browser. After the next local ref
   observation the card moves to Done. Evidence names candidate/target OIDs and observation time.
8. Stop the feed, then resume it. The last board visibly goes stale without losing cards or pretending
   to be live. Show an untagged/missing-verdict example in Unclassified. Cost/findings remain explicitly
   unavailable if those optional sources are absent. No synthetic spend or fake successful history.

No blocking operator questions remain: read-only and board-before-plan-review are decided. Use a
fictional demo root unless the operator separately authorizes showing real project names/content to
the stakeholder. That disclosure choice is the only operator decision needed for a live-data pitch;
it does not block implementation or an isolated demonstration. Changing the default landing page,
adding writable cards or extending the first slice to remote PR polling would be new scope decisions.

Design validation: source and issue reads only; no tests executed, as requested for this design round.
Residual risks for cold read: requirement bookkeeping cost, bounded eventual consistency under a very
busy file bus, unavailable historical independence evidence, and the integration boundary with M4/#206.
