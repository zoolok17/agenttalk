# A work board that moves itself

Status: proposed v4 design for #207, 2026-09-26; supersedes `2967402`. Audience: implementers and cold readers.
This is an explanation and proposed contract, not shipped behavior. Design only; no production change.

The operator decided: read-only, derived from agents' work, board before #206. Card detail becomes a
read-only review surface. Annotation submission stays separate and on hold. The board never sends feedback,
changes tasks, issues GO or authorizes merges. There is no stakeholder demo requirement. Acceptance is the
operator's walkthrough on live data; fixtures and synthetic demonstrations are optional development aids.

## 1. Verified basis and the smaller v1

Code basis: master `01ddb455`, console v2 through `d351359`, and the complete cross-vendor review.
Integrate accepted M4 without touching the frontend seat's active branch. Existing facts constrain v1:

- `Message` wire fields are id/ts/from/to/kind/subject/body/meta. Reuse schema, filename, roster/tombstone
  and signature validation. Never infer workflow from bodies, subjects or seat-name prefixes.
- `_classify_event` distinguishes accepted from terminal done/declined. Declined is not successful work.
  Correlation is echoed by reply transport; other metadata need not be. Unpublished drafts are not events.
- Health can carry exact request/message correlation. CLI flavor is NOT model vendor: Qwen may use Claude CLI.
- gates.json has one overwrite record per gate NAME per root; `revision` is optional today.
- `_compute_keep_floor` can compact closed build/review threads while their work item is still active.
- Attention escalation refs name an esc request, not a work item's build request. An explicit link is needed.
- `/api/state.recent` has only 25 messages. `/api/threads` output pagination does not bound its underlying scan.

V1 has NO off-bus work-item registry, amendment log, sealed review set, board-only scanner or cursor.
Group validated work_item meta already sent. Required reviews are the reviews dispatched for the item's
cycle/candidate. Ready can briefly appear between sequential review dispatches; accept this limitation
because the board reports observed work, not the lead's unstated plan, and is not merge authority.

## 2. Identity, metadata and replacement

Identity is `(project_id, work_item)`. Slug is root-scoped, case-sensitive `[a-z0-9][a-z0-9-]{0,63}`.
Optional `work_title` is bounded to 160 characters; otherwise use originating task subject with provenance.
If no unique originating task is provable, show the slug and an ambiguous-origin reason, not a guessed title.
Conflicting explicit titles are a metadata issue, not a winner selected by timestamp. Similar subjects never join.

Proposed task/review flags: --work-item, --stage, --work-cycle, --work-round, --work-head, --supersedes.
Metadata keys: work_item, work_title, stage, work_cycle, work_round, work_head, supersedes,
external_deliverable, work_repo, work_branch, work_target, required_gates, no_gates_reason.
Reserved publisher-generated key: assignee_model_vendors (per-recipient map, never a scalar or caller override).
external_deliverable accepts only true/false (CLI/meta text normalized to a boolean); absence means false.
True on an opener is a lead-issued read/delta/sweep declaration and requires full work_head and the repository/check
policy below. Replies inherit that declaration, not the authority to issue one. No new external-deliverable flag
is needed; validated --meta suffices.
Escalate also gets --work-item
and --work-cycle, using the SAME validator. Stages: design|build|read|fix|delta|sweep. Cycle/round are positive
decimal integers on CLI/wire, normalized internally. Legacy missing cycle means 1, labelled legacy; missing
round is null. Head is a full Git OID. New syntactically valid slugs require no registration; syntax validation
cannot prevent plausible typos. Do not claim that it does.

Validate equivalent --meta and wrapper draft publication too. Malformed values, conflicting flags/meta,
invalid supersession and cross-root references fail exit 2 before send. Replies inherit item/cycle/stage/head
from their unique opener; contradictory fields are invalid board evidence. Builders may report output OIDs;
a lead review dispatch pins the result for review instead of treating a moving branch as candidate truth.

Verdict parser is case-insensitive: GO/FIX/HOLD for read/delta/sweep, done for design/build/fix. Retire
READY/NOT READY, without aliases. Missing says **verdict missing**; unsupported vocabulary says
**unrecognized verdict**, never success inferred from prose. Existing protocol status is still required:
task-response status=done closes work, accepted does not. Native review-result must have consistent valid
status/verdict: approved/GO, rejected/FIX or HOLD, needs-info/HOLD (nonterminal). Stage comes from opener.

Optional dispatch policy meta: work_repo, work_branch, work_target, required_gates (bounded JSON array of
check keys), no_gates_reason (bounded text, incompatible with nonempty gates). Originating build/design task
defines repository/check policy; for an external-deliverable review-only item, its lead review dispatch defines
that policy. Replacements inherit it. Conflicting parallel policies are Unknown.
Reviews pin the same policy/candidate. Missing policy is unknown, not an empty list. One repo per item in v1;
use separate related items for multi-repository deliverables. These are bus facts, not a second item database.

### Vendor source (F1)

Default independence: **different_seat**. Builder set is EVERY recipient of a validated build/fix dispatch on
the item, across all cycles, including superseded/declined dispatches; conservatively include design authors
when reviewing a design-only cycle. The reviewer must be outside that set. No inference of contributions from
Git authors, completion prose or head ancestry. Missing dispatch/recipient history prevents verified independence.
For a review-only item, lead dispatch meta `external_deliverable=true` plus full work_head and repository/check
policy supplies the deliverable fact without a fabricated build reply; show **external deliverable; authorship
unverified**. With complete item history, independence is checked against the (possibly empty) bus builder set
and labelled **different seat from recorded builders**, not independence from unknown external authors.
Absent the explicit external declaration, no build evidence means Unknown. external_deliverable=true together
with ANY validated build/fix dispatch on the same item (including other cycles, superseded or declined work)
is **Unknown: external deliverable conflicts with recorded build dispatches** (M3); neither evidence path wins.
A later build/fix adds its recipient to the set and makes that mixed provenance Unknown. Unknown model vendor is
**unverified**; CLI/name cannot prove it. A separate CLI badge may describe transport but cannot certify cross-vendor review.

Add operator-set config map `model_vendor: {agent_name: vendor}` alongside roster maps. Closed v1 values:
anthropic|openai|alibaba|other|unverified; missing means unverified. Validate roster membership. Dispatch copies
the value into reserved `assignee_model_vendors`, a map keyed by exact recipients expanded at dispatch time.
Group fan-out copies share that frozen map and request ID; each copy's recipient must occur in the map. Changes
to group membership after dispatch never change the recipient obligation set or vendor map. Missing entries in
legacy history are unverified; contradictory fan-out maps are Unknown. Ordinary task meta cannot override the
map. Retain snapshots after retirement/config changes. Label it operator-configured, not automatic attestation.
Choose config rather than launcher inference because routes vary and transport is misleading. Updating a
model route requires updating config for subsequent tasks. Unknown vendor alone does not block different-seat
review; automatic vendor attestation or mandatory cross-vendor policy is separate scope.

### Legacy, mistakes and supersession (F3)

Untagged legacy openers qualify only for kinds **task** and **review-request**, with at least one outstanding
recipient under existing participant/kind/status rules. Questions, proposals, ordinary messages and reply
envelopes are not extra legacy work items. A needs-info review remains outstanding; valid terminal task
done/declined, review approved/rejected, or requester rescind closes that recipient under the existing classifier.
Deduplicate fan-out by request_id, retaining opener IDs;
ambiguous/missing opener evidence is Unknown rather than a resolved request or an invented exact count.
Internally preserve each thread and its correlation; present ONE **Legacy / untagged work** group with total
open-request count, per-kind counts, as-of/coverage and at most 20 opener-reference examples. This group sits
outside the 100 tagged-card budget, not 120 individual active cards (M2). Its count is not a workflow verdict;
no legacy thread can satisfy a tagged item's build/review obligation without an explicit validated link.
No legacy document bodies are hydrated just to compute the count. The bounded group still counts toward the
256-KiB response limit. Incomplete scans yield an unknown count plus known lower bound, never zero/quiet.
Missing referenced opener, ambiguous correlation, wrong participant or wrong kind means Unknown, never resolved.
No board scan reads draft bodies; only successfully published validated replies count. Refusals may surface
through existing attention. Idle health does not prove completion.

`supersedes=<request_id>` on replacement dispatch works for builds AND reviews, same root/item/cycle.
Only original dispatch authority or current lead/liaison can replace. Validate target; reject self-links,
cycles and ambiguous branching replacements. Old requests/verdicts remain history. Superseding running work
does not cancel it: outstanding execution remains visible and blocks Ready until terminal or rescinded.
Use today's requester-only rescind where authorized; the lead takeover extension below is deferred.
Declined/rescinded work is closed-not-success, not a
permanent blocker; successful replacement satisfies the surviving obligation. All-declined work cannot be
Ready without a successful deliverable and independent review. Never pick latest(stage,round): parallel tasks
can share both. Superseded FIX says awaiting replacement review until successor succeeds; unrelated FIX/HOLD
remains unresolved. A late superseded reply is history/diagnostic, not approval of a replacement candidate.

Typo repair: rescind mistaken dispatch and reissue correctly, optionally with informational corrects_request
(NOT cross-item supersedes). Keep cancelled mistaken dispatch in bus history, not a new board history feed.
No fuzzy merge, signed-message edits
or binding registry. Bulk repair is separate scope.

**Outstanding execution (N4):** every design/build/fix recipient obligation starts outstanding at dispatch,
even without an acknowledgement or fresh health. It ends only on a valid terminal done/declined reply or valid
rescind. Accepted, silence, retirement, lost health and supersedes alone do not end it. Reviews have the same
pending-recipient accounting as outstanding reviews. A queued task is therefore also outstanding execution;
Queued describes evidence of activity, not exemption from Ready checks. Group completion needs all recipients.

**First UI:** honour only today's requester-only rescind accepted by the bus. A current lead's unsupported
rescind cannot cancel another requester's task in the reducer. An outstanding task from an earlier requester
blocks Ready with the exact reason **outstanding task from an earlier requester; current lead cannot cancel yet**.
Superseding it does not lift this barrier. B2c is not a first-UI dependency or acceptance case.

**Later B2c:** a separate protocol slice proposes `rescind --as-lead`: only the CURRENT
configured lead may use it for an earlier requester's task on this root, with an explicit reason and pinned
opener ID. Ordinary responders/liaisons cannot self-promote. Dedicated publication validates the role and emits
a reserved, signed authorization snapshot (actor, role=lead, project_id, opener_id, config digest); callers cannot
supply/override that snapshot through generic send/meta/drafts. Persist the accepted authorization alongside the
rescind envelope so later lead rotation cannot resurrect a cancelled task. Historical imports lacking verified
authorization remain Unknown, not silently trusted. Reuse the existing publication/authentication trust boundary;
the snapshot is an audited authorization decision, not an assertion that CLI-supplied role text proves authority.
Thread reduction, check/scoped-wait supersession barriers, fan-out and attention must recognize the SAME accepted
lead rescind, with skew-safe pinned causal linkage. This is real protocol cancellation, not board-only hiding,
and does not kill a worker process. Exact-request health still reporting execution after cancellation remains an
overlap warning and prevents Ready until it clears. Test old-lead task/new-lead cancellation, subsequent rotation,
unauthorized/snapshot-spoof attempts, group recipients, unanswered dispatch and a still-running cancelled seat.

## 3. Causal reduction and precedence

Reuse existing kind/participant/status rules. The thread walker currently sorts IDs and skips replies whose
ID sorts before opener: do not call it unchanged for clock-skew correctness. The board adapter resolves
request_id/in_reply_to edges first and reuses/extracts _classify_event on causal order, without changing wire
IDs, bus cursors or delivery. Unique correlated terminal replies can follow their opener despite skew.
Ambiguous multi-response sequences without links stay Unknown. Preserve rescind and broadcast obligations.

IDs/timestamps are advisory display order across machines. Supersession/candidate correctness uses explicit
links; incomparable heads are Unknown, not newest-wins. GO applies only to its OID; new heads need renewed
reviews. Highest explicit cycle number on authorized dispatch is current; old obligations remain diagnostics,
never silently successful. Ages are approximate; clamp negative age with a skew indicator. Heartbeats/polls
do not reset workflow age. Deterministic (ts,id) display sorting must not affect correctness.

First matching row wins per cycle. Shared needs-you model adds the top overlay while preserving underlying
placement and evidence. Unknown appears in Unclassified, not disguised as Queued.

| Priority | Proven condition | Column / reason |
|---|---|---|
| 1 | Unresolved canonical operator incident explicitly linked to item/cycle | Needs you; HOLD alone is not human need. Deferral stays visibly deferred here. |
| 2 | Required workflow history missing, conflicting, partial or stale | Unknown, with last-known placement separately stamped. |
| 3 | Candidate integrated in configured target, no outstanding execution; or design completion below | Done; unresolved FIX/HOLD requires **merged with open FIX/HOLD**, warning and review links. Missing review/check evidence is also explicit. |
| 4 | Active fix task, or unresolved FIX without dispatched replacement review | Fix round; dispatched/accepted/running separately labelled. |
| 5 | Accepted or exactly observed-running design/build task | Building, Design badge where applicable; dispatch alone does not prove running. |
| 6 | Outstanding independent read/delta/sweep, including linked replacement review | Independent review; prior FIX/HOLD visible. Unproved independence is labelled unverified review, never certified independent. |
| 7 | Successful deliverable, unique candidate, >=1 independent GO, all surviving dispatched candidate reviews GO, no pending execution/review or unresolved FIX/HOLD, check policy satisfied | Ready: reviewed; no additional review currently dispatched. Mandatory check label below, not permission to merge. |
| 8 | Known stage dispatched without acceptance/execution/completion evidence | Queued / start unconfirmed, not proof the seat has not started. |
| 9 | Otherwise, including missing verdict/candidate/policy, non-operator HOLD, all-declined work, unknown stage | Unknown with exact reason. |

Parallel GO cannot outvote FIX/HOLD. Expose counts during sequential-dispatch Ready fluctuations; do not invent
a seal. Raw merge facts stay visible even when incomplete history forces Unknown. Running requires fresh health,
exact request/message and assignee, not a generic busy-seat label.

Design-only cycles originate with stage=design and have no build dispatch; a fix inherits design purpose only
through explicit supersedes ancestry to that design task. An unlinked fix makes purpose unknown. For these
cycles, future #206 authenticated operator review_verdict=approve
for exact design candidate completes as **design approved**, not merged. Unresolved reviewer FIX/HOLD remains
a warning. Before #206, explicit authorized lead dispatch starting a higher work_cycle may end a terminal
design phase as **design phase ended by lead; operator approval unrecorded**. It cannot close still-active work
or prove approval/merge. Without either event, completed design stays Ready/Unknown. No plan-mode hook.

## 4. Checks and local Git evidence

Choose item-scoped gate NAMES, not a new history database: `wb.<work_item>.c<cycle>.<check>`.
Check keys: [a-z0-9-]{1,24}; full name obeys gates.py's 128-character cap. Require scope
<project_id>/<item>/c<cycle>, revision equal to full candidate OID, plus existing validated green evidence.
Missing revision NEVER satisfies the board. Item A/B cannot overwrite each other. New revision may overwrite
same item's gate; old gate history is unavailable, not invented. A satisfying record uses existing validated
severity=blocker green evidence, not an advisory/warning record relabelled as a passed check.

**Namespace isolation (N1), required before writing ANY wb. gate:** retain gates.json but change gates.py's
selection boundary. An unscoped check excludes wb. names from BOTH required_gates and recorded-gate iteration;
an explicitly non-board scope also excludes them. A board check selects only the dispatched required names
under its exact item/cycle scope, then validates revision/evidence. Global gates remain the ordinary pre-merge
barrier; the board does not replace them. Disallow adding wb. names to the root-wide required_gates configuration;
legacy accidental entries are excluded with a visible configuration warning, not treated as global requirements.
Both web and CLI gate-HOLD attention must consume the same filtered unscoped verdict (no raw-gates back door).
A red wb.a.c2.unit cannot block item B's check --gates or generate team attention. Its failed check is visible on
A's card; it is not automatically an operator incident. Malformed/unreadable gate STATE still fails closed.
This is an intentional gates.py compatibility change, not a claim that current unscoped callers already isolate
names. Reusing gate validation avoids a second check-evidence store; no isolation rollout, no wb. writers.

Later B6b cleanup is explicit lead-side maintenance, NEVER a console GET: preview then remove exact wb. records for
cancelled items or terminal cycles outside the seven-day display window, only if no active request references
that cycle; remove accidental root required-list references in the same locked/atomic update. Preserve an audit
summary of removed name/revision/status and leave all non-wb. gates unchanged. Reopening a pruned cycle requires
fresh check evidence; missing records cannot become green. B6b is outside first UI ONLY because B6a isolation
remains mandatory: unscoped/non-board exclusion, BOTH attention paths and rejection of wb. in root required_gates.
First UI tests red A/green B, missing/wrong revision, required-list contamination and ordinary global blockers;
later cleanup tests concurrent updates and dry-run/exact-prefix removal. Until then old records cause file growth
and gate-list noise, not unrelated verdict changes or stale-green acceptance. CI/no_gates_reason is the common path.

CI usually runs elsewhere. Common explicit policy: required_gates=[] and no_gates_reason="CI tracked outside
local gates". Ready says **independent GO; local checks not tracked (CI external)**, NEVER green CI or checks
passed. Missing list/reason is Unknown. No network CI lookup or PR-badge inference. Optional Git/PR/cost failure
cannot erase known workflow stage: missing merge prevents Done, missing required checks prevents Ready, missing
health removes Running only.

work_repo selects an operator-approved local alias mapped in config to canonical checkout and approved target
refs. This access allowlist is NOT an item registry. Other projects' clones are allowed; never take paths from
bodies/query strings. Reject escapes/symlinks, option-like refs and ambiguous binding. Root repo defaults only
when configured; otherwise unknown.

Cached observer uses only rev-parse, merge-base --is-ancestor, cat-file: shell-free, validated arguments,
GIT_NO_LAZY_FETCH=1, GIT_NO_REPLACE_OBJECTS=1, prompts disabled, -c core.fsmonitor=false. No status, diff,
hooks, fetch, gh, filters or textconv. Read canonical objects, not worktree files. Timeout 2 s/probe, bounded
output, one probe at a time, visible repos refreshed at most every 10 s. No Git in BOARD handlers. Label local
refs as-of time. Missing/shallow objects, timeout, branch movement or repointed alias invalidates evidence.
Ordinary merge/FF: pinned candidate ancestry in configured target proves integration. Closed PR, source branch
deleted or task done does not. Squash/rebase stays unproved in v1; lead-side PR import is optional later scope,
never server network calls. Observer follows first UI cut; until then merge unknown and no code-item Done.

## 5. Shared snapshot, compaction and bounds

Choose **read archived envelopes** (F2). An item-aware keep floor would couple compaction to workflow completion,
pin unrelated history behind old items and fail to recover already compacted FIX replies. Shared snapshot reads
messages/ and archived/compacted/, never silently other archived session trees. Label active/compacted provenance;
revalidate archives, do not trust merely because previously compacted. Strip bodies from board projection. Do not
revive archived messages in delivery or alter compaction.

Support collision filenames <id>.json.<timestamp>: validate embedded ID against original filename and accepted
suffix grammar. Deduplicate identical canonical messages by ID, prefer active provenance; differing contents for
one ID are conflict. Missing referenced opener or required archive read is Unknown. A six-week item retains its
five-week-old FIX. Failed archive refresh cannot yield complete board history; unassignable corruption degrades
board coverage globally. Archive/dedup rules need tests against actual archive_messages_below output.

ONE per-root validated snapshot service supplies state and board. Refactor existing _validated_for_state behind
it: state consumes the active partition with existing semantics; board discovers evidence across BOTH the active
partition and ALL of archived/compacted/. Do not substitute archives reachable from active message IDs: an entirely
compacted item or an unlinked earlier FIX would disappear. State must not replay compacted threads. Nothing changes
delivery or archive retention. The review measured 285 compacted files / about 0.5 MB, so a persistent index is not
a first-UI prerequisite; B4b/B4c remain later performance slices, not permission to omit archives now.

**First-UI discovery/cache (B4a/B4s):** the same service enumerates both partitions and validates all envelopes,
using an in-memory cache keyed by root, canonical file identity/path, size/mtime_ns/ctime_ns and schema/trust
generation. Retain compact workflow fields, validation status and content digest; avoid retaining archive bodies.
Every refresh reconciles membership and stats in BOTH partitions; changed/new files are read and revalidated,
unchanged files reuse validated facts. Trust/config/signing changes invalidate validation even without stat change.
On cold start, read all compacted envelopes before claiming complete coverage. No extra scanner, persistent item
registry or durable index in first UI. Fingerprints are cache invalidation, not protection against an actor who can
rewrite files while preserving all stats. Restart/cache invalidation forces content revalidation.

Do this outside HTTP handlers, at most one coalesced refresh per root per 5 s. Process at most 1,000 files or
250 ms per worker slice, checking cancellation between bounded individual reads, so cold archive work yields to
active /api/state refreshes and other roots. Complete discovery may take multiple slices; expose building/stale
coverage until finished. Reconcile concurrent compaction moves/dedup before publishing a generation. Missing files,
unassignable corruption, changes mid-scan or an unfinished scan cannot turn a partial prefix into Ready/Done.
Preserve state error shapes and independent active-state availability when archive coverage fails.

**Selected closure and cap (M1):** after complete cross-partition discovery, select tagged items that are active
or Done in the seven-day window below. For each selected item include ALL its tagged envelopes across cycles,
every linked request thread and opener/reply/rescind/supersedes dependency, and all-cycle builder facts. Include
untagged envelopes when linked into that closure; they cannot evade its budget. Missing referenced openers remain
Unknown. Entirely archived active items and an old FIX are discovered even with no link from a live opener.
Old terminal item summaries can exclude an item only with complete validated evidence; unknown integration leaves
it potentially active. Reopening or changing the repo target re-evaluates its entire required history.

Cap ONLY this deduplicated selected closure across BOTH partitions at **50,000 envelopes / 128 MiB source bytes**,
not the whole active partition, all archived bytes, or just the first 100 returned cards. Count each message once
using its validated source-envelope byte size; conflicting duplicates degrade coverage. The legacy group in §2
uses aggregate metadata and up to 20 references, not all legacy bodies/threads in the closure. Explicit links from
tagged work still include those threads normally. Unrelated untagged active messages and old closed tagged items
consume scan/cache resources but do not consume the closure budget. This cap protects reduction, not source IO;
/api/state already scans the active partition, whose old unanswered threads may pin the compaction floor.

Coverage reports selected_envelopes, selected_source_bytes and both limits. At **>=60% of either limit** (30,000
envelopes or 80,530,637 bytes) show a persistent capacity warning and tell the lead to schedule B4b/B4c before the
limit is reached. Warning alone does not invalidate complete evidence. Over either limit gives capacity_exceeded
and last-known placement, never a successful truncated reduction. Also report total discovered files/bytes,
refresh duration and cache size: unrelated history can slow discovery even below the closure warning, and a
refresh unable to meet freshness requires earlier indexing. An index does not raise the legitimate selected-work
cap; exceeding that still requires a separately reviewed budget/partition change.

Fresh workflow requires scan-start age <=15 s, current trust generation and a complete reconciled discovery plus
selected closure. Failure retains last-known errors/data and stamps degradation. No quiet/empty claim while
building, stale or over capacity. HTTP polling performs no envelope reads or cache rebuilds.

**Later B4b/B4c:** replace the in-memory discovery cache with a disposable, fingerprinted per-root SQLite archive
index under state/work-board-cache/. Keep request->file and item->request indexes, compact facts, validation/trust
generation and resumable bounded rebuilds. Schema/corruption/disk-full handling must fail visibly. B4c rehydrates
selected dependencies and tests entirely archived items, old unlinked FIX, trust invalidation and changed targets.
It must preserve first UI's all-archive discovery semantics and M1 cap across both partitions; no referenced-only
shortcut. Summaries are rebuildable observation data, never an authoritative off-bus work registry.

GET /api/work-board?root=<id>: <=100 cards, <=256 KiB, no cursor and NO dated-history endpoint in v1 (N7).
Include active items plus Done with last-work-event date within seven UTC days. This is an approximate activity
window, not a claim about merge time. Active means current cycle is not Done and is not wholly cancelled/declined
without remaining work; Unknown items with missing evidence stay active. Never age-filter away active items.
Older Done/cancelled records remain available through existing bus tools, not a new board history service.
Overflow reports truncated=true, known total and omitted count; unknown totals are null with incomplete coverage.
No quiet/empty claim on overflow. >100 included tagged cards requires later filtering/paging. The one counted
legacy group is returned separately and does not inflate total_count/omitted_count or displace tagged cards.
Display sort is approximate
event time+item key, not reducer order. Show the seven-day scope in the UI.

BOARD HTTP does O(returned-card-count) cached projection and zero message-file/Git/network reads. This is
BOARD-scoped, not server-wide: explicit D1/#206 document GET may read one bounded blob. Poll visible board every
5 s, one request in flight, 5 s timeout including JSON, pause while hidden. Selected-root validation matches
existing feeds. Failure retains greyed/stamped cards; stale attention cannot clear a blocker into Ready. No HTTP
writes. Existing transcripts remain on-demand, never loaded as part of board polling.

Proposed v1 feed keys: schema_version, target_root_project_id, generated_at, coverage, items, legacy, total_count,
truncated, omitted_count, errors. Coverage contains active/archive status, scan-start/end, valid-until and the
closure/discovery measurements and warning above. legacy contains open_request_count (nullable), known_lower_bound,
counts_by_kind, as_of, coverage, examples (<=20 opener references), truncated; no individual legacy workflow cards.
Each item: id, work_item, title, cycle, round, workflow_column, reason, last_known_column, candidate, tasks,
seats, attention_refs, evidence_refs, first_dispatch_at, last_work_event_at, checks, findings, cost, merge, issues.
Unknown is null, not zero success. Workflow columns: queued/building/independent_review/fix_round/ready/done/unknown;
shared client overlay adds needs_you. Merge: integrated/not_integrated/unknown. Coverage: complete/building/stale/
unavailable/capacity_exceeded. Findings initially {count:null,status:"unavailable"}; cost null. Detail GET takes root
and validated slug, <=100 task stubs and bounded refs with truncation. Output limits NEVER discard unresolved facts
from reduction. Closed feed schema/fixture assertions belong in B6; no bus bodies or filesystem paths in list.

## 6. Needs-you, card and read-only detail

B7 and B8 start only after console v2 is merged into master at an explicitly recorded SHA; neither edits or
extracts code from the frontend seat's unmerged branch (N3). Extract the merged needs-you calculation once for
stream/board. Escalate flags and build_attention publish exact
validated item/cycle with sanitized source_refs. Resolve membership from escalation opener, not by pretending esc
IDs are build IDs. Deduplicate attention/chat by escalation ID. Unlinked incidents remain team attention; do not
spray a seat warning across its work. Stuck mapping requires fresh exact task correlation. Later/Wait defer local
presentation, never resolve workflow; both views show the same incident and deferral state.

Cards show title/reason, seats, configured model vendor or unverified, explicit round or unknown, open/total
obligations, approximate first-dispatch and last-work-event ages. Evidence explains placement. No guessed finding
counts from Markdown. Cost later can join exact gateway child (agent,message_id,request_id) and deduplicated
attempt/generation IDs. Settled spend and reserved/uncertain exposure remain separate; existing child-turn exposure
is not spend. Subscriptions do not imply EUR and absent cost is not zero. Adapter is optional, not first-board scope.

Vanilla JS, textContent, same CSP as /dashboard, no style attribute, fixed local links, loopback and GET-only.
Reconcile keyed cards in place, retain focus/scroll and announce moves politely. One router owns #board,
#conversation and reserved #review=<escalation-id>; preserve ?root=. Malformed fragments safely fall back. No new
/ default. Board detail initially shows evidence; D-series follows first B-series delivery.

## 7. #206 integration notes (C1-C9)

#206 remains on hold. These are contracts for its later revision, not edits to that branch in this round.

| ID | Integration |
|---|---|
| C1 | Review escalation adds work_item/work_cycle to review_doc/review_sha/review_series. Series stays per-document within item. |
| C2 | Finding identity = <reply message id>#<finding id>; item/cycle from review opener, candidate from finding.sha. No second identity or global F1 namespace. |
| C3 | No per-finding closure in v1. Open findings belong to unsuperseded required FIX/HOLD reviews for current candidate. GO/superseded findings are history; pending replacements remain visible when old findings leave active count. |
| C4 | Authenticated #206 approve at exact design candidate completes as design approved. Explicit next-cycle lead dispatch may end a terminal design phase, labelled without human approval. Neither is merge proof. |
| C5 | review_repo uses same approved alias/hardened Git helper. plan_review.paths remains an additional document allowlist per alias; default root only when configured. |
| C6 | Zero reads is BOARD-scoped. Explicit document GET may do one timed/bounded cat-file read, no arbitrary filesystem or network reads. |
| C7 | deriveNeeds owns PLAN REVIEW from sanitized additive review object, common item/cycle refs and deferral. One attention sanitizer; land board correlation first. |
| C8 | Later #206 skill builds on board skill; escalation/fix carry item/cycle and canonical verdict/status. Operator marks/dispositions remain #206 records. |
| C9 | One router owns #board/#conversation/#review=<id>; board detail is local selection under #board. No competing hash handlers. |

### D1-D3, strictly after first B-series delivery

D1: document at approved alias/path/full SHA, plain safe text with line numbers. Validate path against document
allowlist and tree/blob, not worktree. Bounded cat-file only. Copy passage with reference emits item, request ID,
alias, path, SHA, lines and exact quote. Clipboard failure leaves selectable text. Local copying sends nothing.
Blob <=256 KiB; selection <=8 KiB. Explicit too-large error, not deceptively complete truncation.

D2: changed since you looked uses browser-local last-viewed SHA keyed by root/item/repo/path and labelled as such.
Capture previous baseline before recording current visit; polling does not advance it. Opening is not approval.
Missing baseline/object offers explicit revision selection. Compare two bounded approved blobs with a bounded
in-process text diff (not git diff/helpers); enforce line/work limits and explicit oversize refusal. Comparison is
not an editor or automatic finding re-anchoring.

D3 later: require #206 stable finding records, C2 identity and C3 counting. Match exact SHA/path/quote; moved,
outdated/unknown anchors stay labelled, never drawn on unrelated lines. No resolve checkboxes, approve-anyway,
redlines or submission. Feedback remains existing operator chat/escalation answers. V2 composer cannot send today:
do not prefill an unusable workflow. #206 write path remains separate and requires its own authorization.

## 8. Universal lead skill addition (proposed)

Apply identical semantics to both shipped lead skills; platform-specific shell examples only.

> Reuse one stable work_item slug through design/build/read/fix/delta/sweep; tag cycle/round explicitly. Preserve
> unknown legacy history. Use validated flags when available, equivalent --meta otherwise. Replacement builds AND
> reviews carry supersedes=<old request id>; rescind still-running work when cancellation is needed. Similar
> subjects never prove replacement.
> A never-answered dispatch is outstanding too; first UI honours requester-only rescind. For an earlier requester,
> show "outstanding task from an earlier requester; current lead cannot cancel yet"; replacement is not cancellation.
>
> Pin candidate and approved repo alias before review. Dispatch intended parallel reads before interpreting GO:
> the board knows only dispatched reviews. Reviews use GO/FIX/HOLD case-insensitively; design/build/fix use done.
> Retire READY/NOT READY. Require existing typed terminal status too. Missing verdict is not success. GO/done is
> not merged.
> Review-only human/external work needs external_deliverable=true, full work_head and repository/check policy;
> combined with any recorded build/fix dispatch on the item it is Unknown, not a shortcut past build evidence.
> Independence uses all recorded build/fix recipients on the item, across cycles, not just the latest author.
>
> Default to different-seat independence. Model vendor requires operator-configured model_vendor snapshotted at
> dispatch, never cli/name. Unknown is unverified. Local gates need item-scoped names and exact candidate revision.
> Group dispatch freezes publisher-owned assignee_model_vendors for the exact recipients, never a scalar or override.
> Do not create wb. gates before B6a isolation; never put them in root required_gates. Cleanup awaits later B6b.
> Otherwise give no_gates_reason (often external CI); do not claim unseen CI verified.
>
> Escalate with --work-item/--work-cycle for common incident placement. Never clear FIX/HOLD through prose or an
> unrelated GO. Correct slugs by cancelling/reissuing, not editing messages. Keep local merge evidence, existing
> project planners and irreversible-action gates. Ready remains advisory; the board only describes work.

### B2a follow-up to built `b54c05c` (B2a-f)

The built B2a already includes the four section-8 themes; synchronize their exact contracts rather than append
duplicate advice. This design does not change that accepted implementation directly. The small follow-up must:

1. Add external_deliverable to work_tags' validated key set with strict true/false normalization, including
   equivalent --meta and wrapper publication. For true on an opener require a lead-issued review stage, full work_head,
   work_repo/work_branch/work_target and explicit check policy (nonempty required_gates, or [] plus
   no_gates_reason). Reject malformed/missing declarations before send; keep malformed historical evidence Unknown.
2. Preserve external_deliverable through replacement policy inheritance and unique-opener reply inheritance;
   reject contradictory overrides. Do not copy one item's declaration onto unrelated dispatches. The reducer
   owns M3's whole-history mixed-provenance check; publication validation alone cannot certify it.
3. Reserve the exact name assignee_model_vendors for the publisher's per-recipient map. Reject generic
   send/meta/draft overrides and the obsolete assignee_model_vendor scalar; B2b implements trusted config
   snapshot generation and map/fan-out validation. Do not add a public scalar/map-setting task flag.
4. Synchronize BOTH shipped lead skills with the four revised instructions above: requester-only cancellation
   and blocked reason; external declaration prerequisites/mixed provenance; exact reserved per-recipient map;
   B6a gate isolation with B6b cleanup explicitly deferred. Preserve the all-cycle builder independence rule.
5. Add focused metadata/publication/draft/replacement/reply fixtures for true/false, absent head/policy,
   contradictory inheritance and reserved-map/scalar injection; update the step record/changelog. No reducer,
   group snapshot generator, lead-rescind or cleanup implementation in this follow-up.

## 9. Delivery slices and acceptance

Estimates are changed lines including focused tests and documentation, not promises of a fixed diff size.
Split before implementation if a slice exceeds about 700 lines. No production work is authorized by this design.
B1 (registry) is removed. B3 is explicitly split; adversarial tests are required before accepting the board.

| Slice | Builder | Scope and acceptance | Estimated lines |
|---|---|---|---:|
| B2a | Python developer; built and cold-read GO | Validated task tags, reply normalization, supersedes and lead skill at b54c05c. | 533 actual |
| B2a-f | Python developer | Exact follow-up in §8: external declaration validation/inheritance, reserved map name, both skills and focused parity/spoof fixtures. | 100–180 |
| B2b | Python developer | Operator model_vendor config/per-recipient dispatch map; escalation item flags and attention projection. Test mixed-vendor group fan-out, membership change, qwen routed through claude, unverified vendor, spoofing and exact escalation placement. | 250–400 |
| B2c | Python protocol developer, LATER | Lead-authorized rescind with durable authorization; same semantics in publication, threads, check, wait and attention. Separate compatibility review. First UI tests only existing requester-only semantics and the earlier-requester Ready blocker. | 400–650 |
| B3a | Strong Python developer | Pure causal reducer and basic real-envelope fixtures. Each decision row, native thread classification parity, unknown and revision cases. | 350–500 |
| B3b | Python developer/test specialist | Adversarial reducer fixtures: skew/parallel verdicts, requester-only cancellation and earlier-requester block, groups/builders, M3 mixed external/build Unknown, supersession, head changes, merged-with-FIX, design completion and Ready between reads. | 400–600 |
| B4a | Python IO developer | Shared per-root snapshot, active-state parity, coalescing, generation invalidation and selected-closure cap across BOTH partitions. No whole-active cap or duplicate scanner. | 350–500 |
| B4s | Python IO developer | First-UI ALL-compacted-envelope discovery with in-memory fingerprint cache. Old unlinked FIX/entirely archived item, compaction/dedup, missing opener, 60% warning, legacy group and both-partition scale/error fixtures. | 250–400 |
| B4b | Python IO developer, LATER | Disposable fingerprinted archive index in the SAME service. Bounded batches/checkpoints, request/item indexes, schema/trust invalidation, interrupted rebuild and disk-full. Preserve B4s discovery semantics. | 450–650 |
| B4c | Python IO/test developer, LATER | Indexed dependency hydration and scale fixtures: archived active item, unlinked FIX, all-cycle builders, missing opener, reopening/target changes and cap semantics across BOTH partitions. | 200–300 |
| B6a | Python developer | Bounded active-plus-seven-day-Done feed and item gate adapter/isolation. Test unscoped check --gates, BOTH attention paths, root requirements, cross-item red, exact revision, global blockers, truncation and external CI. No history endpoint/cursors. | 400–600 |
| B6b | Python developer, LATER | Explicit lead-side board-gate cleanup, audited preview/atomic prune. Test exact names, terminal eligibility, referenced active cycle, concurrent update and retained non-board gates. Safe to defer ONLY with B6a isolation shipped. | 200–300 |
| B7 | Frontend developer AFTER console v2 merge to master | Shared needs-you projection and incident identity. Record merge SHA; stream/board parity, Later, resolved/stale/unmapped incidents, new turns and future plan-review object. | 200–350 |
| B8 | Frontend developer AFTER console v2 merge to master | Pure console model, keyed cards/detail/router and operator walkthrough. Real-envelope Node fixtures and browser focus/scroll/keyboard, GET/CSP, stale/Unknown, unknown merge/cost/findings. No document viewer. | 500–700 |
| B5 | Python developer, after first UI | Restricted local Git observer and integration evidence. Allowlist, hostile refs/env, no helper/network invocation, timeout, shallow history, missing objects and merged-with-FIX tests. | 300–500 |
| D1 | Python + frontend, after B-series | Pinned document and copy-with-reference detail. Traversal/object/blob limits, safe rendering, exact reference/quote and clipboard fallback browser tests. | 500–700 |
| D2 | Frontend, after D1 | Browser-local comparison baseline and bounded plain-text diff. First visit, reload, polling, storage failure, unavailable revisions and large diff tests. | 400–600 |
| D3 | Frontend, after stable finding contract | Read-only finding marks with C2/C3 identity, supersession and anchor tests; provisional estimate pending that contract. | 450–700 |

First UI is exactly **B2a, B2a-f, B2b, B3a, B3b, B4a, B4s, B6a, B7, B8**: **3,333–4,763 lines** including
B2a's actual 533, or **2,800–4,230 remaining** after that built slice. Rounded planning range: 3,300–4,800.
The difference from the reviewer's 3,100–4,700 estimate includes the now-explicit B2a follow-up and actual B2a size.
B2c/B4b/B4c/B6b are NOT first-UI prerequisites or acceptance obligations. B6a isolation is indispensable.
B6a depends on B3a/B3b and B4a/B4s; acceptance also needs B2a-f/B2b/B7/B8. B7/B8 still wait for a recorded
console-v2 merge SHA on master. B5 may follow initial UI with merge explicitly Unknown.
D-series starts after first B-series UI acceptance and B5's restricted Git helper, not after every deferred
protocol/index/cleanup enhancement. None is hidden inside B8. Cost aggregation and PR imports remain optional.

Performance acceptance uses at least 11,705 active real-shaped envelopes (~36.4 MB), 500 historical items and
20 seats; add >50,000 / >128-MiB UNRELATED envelopes in each partition without exhausting the selected-closure cap.
Include 120 outstanding untagged task/review openers: one counted legacy group, no loss of tagged card slots.
Test 60% warning and true selected-closure overflow separately; fully archived items and unlinked earlier FIX
must remain visible. Record cold full discovery separately from warm refresh; a one-file addition/change reparses
only affected envelopes, trust changes invalidate evidence and warm polls do not rehash all unchanged history.
Measure warm board HTTP p95 below 50 ms on the recorded development machine, with zero source/Git reads per request.
Record shared refresh duration, source bytes/reads and peak memory separately; the 50 ms target is NOT a full-scan
budget. Concurrent state/board polling must coalesce refreshes; oversized or slow refreshes must expose incomplete
or stale coverage without implying Ready. Use deterministic cap/error tests as well as the measured workload.

The operator's walkthrough on their own live data is acceptance: identify known items and legacy Unknowns; observe
published replies changing cards; check parallel reads and replacement tasks; compare a linked escalation in both
views; inspect stale and missing evidence; navigate detail without losing focus/scroll. Before B5 merge is Unknown;
after B5 compare the pinned candidate against the configured local target and inspect warning cases. No invented
activity, scripted stakeholder demonstration or fictional root is required. Synthetic fixtures remain test inputs.

## 10. Review disposition and remaining limits

| Review | Disposition and change |
|---|---|
| F1 | ACCEPTED: different-seat default, operator config fact snapshotted at dispatch, unverified badges until supplied (§2). |
| F2 | ACCEPTED: validated archived envelopes in the shared snapshot; missing referenced opener is Unknown (§5). |
| F3 | ACCEPTED: explicit supersedes for builds and reviews; declined work is not an eternal Ready prerequisite (§2–3). |
| F4 | ACCEPTED: item/cycle gate names plus mandatory exact revision; external CI normally has an honest no_gates_reason (§4). |
| F5 | ACCEPTED: escalate item/cycle flags and build_attention projection, not guessed request correlation (§2, §6). |
| F6 | ACCEPTED: GO/FIX/HOLD versus done, case-insensitive, verdict missing visible; READY/NOT READY retired (§2). |
| F7 | ACCEPTED: causal references drive state; writer-clock IDs only break display ties; ambiguity stays Unknown (§3). |
| F8 | ACCEPTED: proven integration can say Done while prominently preserving open FIX/HOLD and missing checks (§3–4). |
| F9 | ACCEPTED: bounded allowlisted Git plumbing, disabled helpers/lazy fetching/replacements; no status/diff invocation (§4, §7). |
| O1–O4 | ACCEPTED: no item registry/seal/amendment system, one shared snapshot, bounded list without cursors, supersedes metadata (§1–5). N7 further removes the dated-history feed. |
| Sizing | ACCEPTED: separate reducer/adversarial tests, archive work and UI/Git phases; explicit total range (§9). |
| C1–C9 | ACCEPTED as integration contract notes (§7); held #206 is not modified here. |
| D1–D3 | ACCEPTED as separate follow-ons; read-only document/copy, local comparison and later stable finding marks (§7, §9). |
| N1 | ACCEPTED: reserve wb. gates, exclude them from unscoped/non-board checks and both attention callers. B6a isolation stays a rollout prerequisite; audited cleanup is later B6b (§4). |
| N2 | ACCEPTED with round-3 ruling: first UI discovers ALL compacted envelopes with in-memory fingerprints; persistent index/hydration B4b/B4c deferred. M1 budgets only selected closure across BOTH partitions (§5). |
| N3 | ACCEPTED: B7 and B8 depend on a recorded console-v2 merge SHA on master (§6, §9). |
| N4 | ACCEPTED: unanswered dispatch is outstanding; first UI honours requester-only rescind and explains the earlier-requester blocker. Lead-authorized rescind is later B2c (§2). |
| N5 | ACCEPTED: all-cycle build/fix recipient set, conservative design authors, and explicit external-deliverable provenance for review-only items (§2, B3b). |
| N6 | ACCEPTED: reserved per-recipient vendor map frozen across group fan-out; missing/conflicting maps are unverified/Unknown (§2, B2b). |
| N7 | ACCEPTED: v1 contains active items and seven-day Done only; no dated history endpoint (§5, B6a). |
| M1 | ACCEPTED: 50k/128-MiB limit covers selected tagged work plus linked dependencies across BOTH partitions, never the entire active partition; 60% warning schedules indexing (§5). |
| M2 | ACCEPTED: outstanding untagged task/review-request threads become one counted Legacy group outside 100 tagged cards; bounded references, no body hydration for counting (§2, §5). |
| M3 | ACCEPTED: external declaration plus any item build/fix dispatch is Unknown, including cross-cycle/superseded/declined dispatches (§2, B3b). |
| Round-3 cuts | ACCEPTED: B2c/B4b/B4c/B6b leave first UI; requester-only rescind, full archive discovery and B6a isolation remain. B2a-f names the exact metadata/skill follow-up (§8–9). |

No disagreement with the binding decisions and no blocking operator question. First UI reads all compacted history
with a shared in-memory cache; it avoids changing retention semantics or losing evidence already compacted.
Discovery time/cache memory still grow with unrelated history: the closure cap is not an IO limit, and indexing
must precede freshness/capacity pressure. A long-lived selected item can exceed the budget; unrelated history cannot.
Explicit supersession is preferred to latest(stage, round), which would silently erase parallel reviewers.
The known trade-off of no registry/seal is visible temporary Ready between separately dispatched reviews; Ready is
an observation, never a merge permission. Plausible slug typos require correction through ordinary bus work.
Operator-set vendor is an assertion, not provider attestation. External CI, PR state, unknown costs and missing
finding IDs remain explicitly unavailable until actual evidence adapters exist. Document viewing/local comparison
never constitutes approval and never publishes feedback.
