# A work board that moves itself

Status: proposed v2 design for #207, 2026-09-26; replaces `d232eac`. Audience: implementers and cold readers.
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
Metadata keys: work_item, stage, work_cycle, work_round, work_head, supersedes. Escalate also gets --work-item
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
defines repository/check policy; replacements inherit it. Conflicting parallel policies are Unknown.
Reviews pin the same policy/candidate. Missing policy is unknown, not an empty list. One repo per item in v1;
use separate related items for multi-repository deliverables. These are bus facts, not a second item database.

### Vendor source (F1)

Default independence: **different_seat**, reviewer differs from every contributing builder for that candidate.
Unknown builder attribution prevents verified independence. Unknown model vendor is **unverified**; CLI/name
cannot prove it. A separate CLI badge may describe transport but cannot certify cross-vendor review.

Add operator-set config map `model_vendor: {agent_name: vendor}` alongside roster maps. Closed v1 values:
anthropic|openai|alibaba|other|unverified; missing means unverified. Validate roster membership. Dispatch copies
the assignee's value into reserved assignee_model_vendor; ordinary task meta cannot override it. Retain old
dispatch snapshots after retirement/config changes. Label it operator-configured, not automatic attestation.
Choose config rather than launcher inference because routes vary and transport is misleading. Updating a
model route requires updating config for subsequent tasks. Unknown vendor alone does not block different-seat
review; automatic vendor attestation or mandatory cross-vendor policy is separate scope.

### Legacy, mistakes and supersession (F3)

Untagged openers stay isolated as legacy:<opener-id> in Unclassified. Correlated replies inherit grouping.
Missing referenced opener, ambiguous correlation, wrong participant or wrong kind means Unknown, never resolved.
No board scan reads draft bodies; only successfully published validated replies count. Refusals may surface
through existing attention. Idle health does not prove completion.

`supersedes=<request_id>` on replacement dispatch works for builds AND reviews, same root/item/cycle.
Only original dispatch authority or current lead/liaison can replace. Validate target; reject self-links,
cycles and ambiguous branching replacements. Old requests/verdicts remain history. Superseding running work
does not cancel it: outstanding execution remains visible and blocks Ready until terminal or rescinded.
Use existing rescind where cancellation is needed. Declined/rescinded work is closed-not-success, not a
permanent blocker; successful replacement satisfies the surviving obligation. All-declined work cannot be
Ready without a successful deliverable and independent review. Never pick latest(stage,round): parallel tasks
can share both. Superseded FIX says awaiting replacement review until successor succeeds; unrelated FIX/HOLD
remains unresolved. A late superseded reply is history/diagnostic, not approval of a replacement candidate.

Typo repair: rescind mistaken dispatch and reissue correctly, optionally with informational corrects_request
(NOT cross-item supersedes). Keep cancelled mistaken card in history. No fuzzy merge, signed-message edits
or binding registry. Bulk repair is separate scope.

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
same item's gate; old gate history is unavailable, not invented. Global gates retain existing meaning elsewhere.

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
it: state consumes active partition with existing semantics, board consumes active+compacted. Archive caching is
inside that SAME service; no second scanner or independent validator. State must not replay compacted threads.
Single refresh job/root, coalesced across clients; reuse unchanged file fingerprints and cached archive data.
Directory fingerprints detect membership, not in-place edits: stat-check files and fully revalidate content and
signatures at least every 60 s. Config/signing/retirement changes invalidate trust immediately. Compaction changes
both partitions: mid-build membership change requires retry/incomplete coverage, not half a move as resolved.
Publish snapshots atomically; observation interval is not an atomic bus transaction or merge authority.

Warm refresh at most once per 5 s, outside handlers. Failure retains last-known data/errors, never stamps it fresh.
Fresh workflow requires scan-start age <=15 s and complete required partitions. Active state can remain usable
while archives fail; board exposes degradation. Preserve existing state consumers' error shapes and test parity.
Slow refresh yields stale, not fake live latency. Input cap: 50,000 records/128 MiB source bytes per root; reaching
either means capacity_exceeded, not silently complete prefix. Bound file reads before parsing; oversized records
are coverage errors. One worker services roots fairly, cancellation/budget checks between files. No public
cooperative iterator/cursor protocol. Measure on observed ~11,400 active records/60 MB plus archives, not a claimed
25 ms full scan. Cache is disposable; errors never block message publication.

GET /api/work-board?root=<id>: <=100 active cards, <=256 KiB, no cursor. Dated history uses from=<UTC-date>&to=<UTC-date>,
max 31 days, same caps. Never age-filter away active work. Overflow reports truncated=true, known total and omitted
count; no quiet/empty claim. >100 active cards requires later filtering/paging. Display sort is approximate event
time+item key, not reducer order. History ranges use approximate last-work-event time and are labelled accordingly.
Active means current cycle is not Done and is not wholly cancelled/declined without remaining work. Unknown
items with missing evidence stay active; proven terminal/cancelled items remain available in dated history.
Also include Done items whose last-work-event date is within seven UTC days, counted within the same response
bounds. This approximate activity window is not a claim about merge time; dated history retains older Done items.

BOARD HTTP does O(returned-card-count) cached projection and zero message-file/Git/network reads. This is
BOARD-scoped, not server-wide: explicit D1/#206 document GET may read one bounded blob. Poll visible board every
5 s, one request in flight, 5 s timeout including JSON, pause while hidden. Selected-root validation matches
existing feeds. Failure retains greyed/stamped cards; stale attention cannot clear a blocker into Ready. No HTTP
writes. Existing transcripts remain on-demand, never loaded as part of board polling.

Proposed v1 feed keys: schema_version, target_root_project_id, generated_at, coverage, items, total_count,
truncated, omitted_count, errors. Coverage contains active/archive status, scan-start/end and valid-until.
Each item: id, work_item, title, cycle, round, workflow_column, reason, last_known_column, candidate, tasks,
seats, attention_refs, evidence_refs, first_dispatch_at, last_work_event_at, checks, findings, cost, merge, issues.
Unknown is null, not zero success. Workflow columns: queued/building/independent_review/fix_round/ready/done/unknown;
shared client overlay adds needs_you. Merge: integrated/not_integrated/unknown. Coverage: complete/building/stale/
unavailable/capacity_exceeded. Findings initially {count:null,status:"unavailable"}; cost null. Detail GET takes root
and validated slug, <=100 task stubs and bounded refs with truncation. Output limits NEVER discard unresolved facts
from reduction. Closed feed schema/fixture assertions belong in B6; no bus bodies or filesystem paths in list.

## 6. Needs-you, card and read-only detail

Extract existing needs-you calculation once for stream/board. Escalate flags and build_attention publish exact
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
>
> Pin candidate and approved repo alias before review. Dispatch intended parallel reads before interpreting GO:
> the board knows only dispatched reviews. Reviews use GO/FIX/HOLD case-insensitively; design/build/fix use done.
> Retire READY/NOT READY. Require existing typed terminal status too. Missing verdict is not success. GO/done is
> not merged.
>
> Default to different-seat independence. Model vendor requires operator-configured model_vendor snapshotted at
> dispatch, never cli/name. Unknown is unverified. Local gates need item-scoped names and exact candidate revision.
> Otherwise give no_gates_reason (often external CI); do not claim unseen CI verified.
>
> Escalate with --work-item/--work-cycle for common incident placement. Never clear FIX/HOLD through prose or an
> unrelated GO. Correct slugs by cancelling/reissuing, not editing messages. Keep local merge evidence, existing
> project planners and irreversible-action gates. Ready remains advisory; the board only describes work.

## 9. Delivery slices and acceptance

Estimates are changed lines including focused tests and documentation, not promises of a fixed diff size.
Split before implementation if a slice exceeds about 700 lines. No production work is authorized by this design.
B1 (registry) is removed. B3 is explicitly split; adversarial tests are required before accepting the board.

| Slice | Builder | Scope and acceptance | Estimated lines |
|---|---|---|---:|
| B2a | Python developer | Validated task tags, reply normalization, supersedes and lead skill. Invalid/conflicting metadata, missing verdict, retired vocabulary, draft/publication parity, declined replacement tests. | 400–600 |
| B2b | Python developer | Operator model_vendor config/dispatch snapshot; escalation item flags and attention projection. Test qwen routed through claude, unverified vendor, reserved-field spoofing and exact escalation placement. | 250–400 |
| B3a | Strong Python developer | Pure causal reducer and basic real-envelope fixtures. Each decision row, native thread classification parity, unknown and revision cases. | 350–500 |
| B3b | Python developer/test specialist | Adversarial reducer fixtures and necessary corrections: shuffled/skewed IDs, parallel GO/FIX/HOLD, decline/re-dispatch, branching supersedes, cycle/head changes, merged-with-FIX, design completion and temporary Ready between reads. | 400–600 |
| B4a | Python IO developer | Shared per-root validated-envelope snapshot serving state and board. Coalescing, active-state parity, trust invalidation, edited files, errors, source limits and no duplicate scans. | 350–550 |
| B4b | Python IO developer | Archive partition in the SAME snapshot service. Closed FIX on a live item, suffixed archive filenames, duplicate/conflicting IDs, mid-compaction moves, missing/archived opener, tampering and excluded reset sessions. | 250–400 |
| B6 | Python developer | Bounded board/history feeds and item gate adapter. Test overflow, old live items, root isolation, path redaction, parallel item gate names, absent revision and external-CI reason. No cursors. | 300–500 |
| B7 | Frontend developer | Shared needs-you projection and incident identity. Stream/board parity, Later, resolved/stale/unmapped incidents, new turns and future plan-review object. | 200–350 |
| B8 | Frontend developer after console M4 | Pure console model, keyed cards/detail/router and operator walkthrough. Real-envelope Node fixtures and browser focus/scroll/keyboard, GET/CSP, stale/Unknown, unknown merge/cost/findings. No document viewer. | 500–700 |
| B5 | Python developer, after first UI | Restricted local Git observer and integration evidence. Allowlist, hostile refs/env, no helper/network invocation, timeout, shallow history, missing objects and merged-with-FIX tests. | 300–500 |
| D1 | Python + frontend, after B-series | Pinned document and copy-with-reference detail. Traversal/object/blob limits, safe rendering, exact reference/quote and clipboard fallback browser tests. | 500–700 |
| D2 | Frontend, after D1 | Browser-local comparison baseline and bounded plain-text diff. First visit, reload, polling, storage failure, unavailable revisions and large diff tests. | 400–600 |
| D3 | Frontend, after stable finding contract | Read-only finding marks with C2/C3 identity, supersession and anchor tests; provisional estimate pending that contract. | 450–700 |

First UI (B2a–B4b, B6–B8) totals roughly 3,000–4,600 changed lines. B5 adds 300–500.
This is a planning range, not an assurance that archive correctness fits the low end. B6 depends on B3a/B3b and
B4a/B4b; first UI acceptance also needs B2a/B2b and B7. B5 may follow initial UI with merge explicitly Unknown.
D-series begins after B-series acceptance, including B5; none is hidden inside B8. Cost aggregation and PR imports
are optional later slices, not unfinished first-board obligations.

Performance acceptance uses at least 11,400 real-shaped active/archive envelopes, 500 historical items and 20 seats.
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
| O1–O4 | ACCEPTED: no item registry/seal/amendment system, one shared snapshot, bounded list/history without cursors, supersedes metadata (§1–5). |
| Sizing | ACCEPTED: separate reducer/adversarial tests, archive work and UI/Git phases; explicit total range (§9). |
| C1–C9 | ACCEPTED as integration contract notes (§7); held #206 is not modified here. |
| D1–D3 | ACCEPTED as separate follow-ons; read-only document/copy, local comparison and later stable finding marks (§7, §9). |

No disagreement with the binding decisions and no blocking operator question. The chosen archive approach costs an
additional shared snapshot slice; it avoids changing retention semantics or losing evidence already compacted.
Explicit supersession is preferred to latest(stage, round), which would silently erase parallel reviewers.
The known trade-off of no registry/seal is visible temporary Ready between separately dispatched reviews; Ready is
an observation, never a merge permission. Plausible slug typos require correction through ordinary bus work.
Operator-set vendor is an assertion, not provider attestation. External CI, PR state, unknown costs and missing
finding IDs remain explicitly unavailable until actual evidence adapters exist. Document viewing/local comparison
never constitutes approval and never publishes feedback.
