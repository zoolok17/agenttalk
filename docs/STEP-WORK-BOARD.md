# Work-board implementation record

Audience: implementers and cold reviewers. This records shipped slices, not a
claim that the board reducer, feeds or UI exist.

## B2a — tags, verdicts, supersedes and lead skill

Base: master `1f407087`. Contract: design/work-board `2967402f`, sections 2, 8
and the B2a row in section 9. Branch: `feat/work-board-b2a`.

| Contract | Implementation / evidence |
| --- | --- |
| Task tags | `task --work-item --stage --work-cycle --work-round --work-head --supersedes`; equivalent `--meta` uses the same validator. Conflicting flags or duplicate work metadata refuse exit 2 before send. Optional metadata `work_title` is bounded to 160 characters. |
| Wire normalization | Lowercase slug, closed stage set, positive decimal cycle/round and full 40/64-character Git OIDs. Integers and decimal text normalize to canonical decimal text; missing cycle stays absent (legacy cycle 1), missing round stays absent. |
| Replies | Shared Store publication validates unique opener, participants, kind and correlation; inherits item/cycle/stage/round/head. Builders may report an output OID; review replies cannot change their candidate. Untagged legacy replies retain their existing protocol. |
| Verdicts | Case-insensitive GO/FIX/HOLD for read/delta/sweep and done for design/build/fix. `verdict_issue` explicitly reports `verdict missing` or `unrecognized verdict`; READY/NOT READY have no aliases. Native review status must agree with a recognized verdict. Missing status is never invented. |
| Draft parity | Body-only task drafts keep their existing `status=done` but cannot supply a verdict. They publish the same missing-verdict marker as a CLI task-response with status=done and no verdict. No body parsing. Native review drafts remain unavailable because their typed evidence cannot be carried by this channel. |
| Replacement | Same-root/item/cycle dispatch target, original authority or current lead/liaison, fresh request ID, no self/cycle/branch. Recheck under the existing publication mutex excludes competing branches. Repository/check-policy metadata is inherited; conflicting replacement policy refuses. Old requests, declined replies and outstanding execution remain history. |
| Skills | Identical universal protocol section in Claude/Codex lead twins. Later vendor, escalation-flag, gate-isolation and cancellation capabilities are explicitly conditional on their slices shipping. |

Interpretations and boundaries: unknown/retired verdict vocabulary is published
as diagnostic evidence, not silently aliased or inferred from prose. Publication
does not turn a status=done message with a missing verdict into board success;
the future reducer must honor the issue and existing typed status. Missing stage
cannot give a recognized verdict. Cycle defaults are used for comparison without
erasing the legacy absence on the wire. Canonical text avoids representation
conflicts between flags and metadata.

Supersession uses this root's existing validated live-message reader. An opener
that is unavailable or already compacted refuses; archive hydration belongs to
B4, and no second scanner or registry is introduced. The existing rescind
`supersedes` generation-object namespace is preserved; B2a does not implement
cancellation. Optional repository/check policies are inherited as declarations;
repository allowlists and check evaluation belong to later slices. No vendor
configuration, escalation linking, reducer, snapshot, feed, UI or merge authority
is introduced here.

### Executed validation

All tests run foreground with `PYTHONPATH=<worktree>/src`, isolated task scratch
for TEMP/TMP and basetemps, and `-q -p no:cacheprovider`. No full suite or worker
process launches. `tests/test_work_tags.py` is synthetic bus/CLI integration.

Failing-first: 39 failed, 5 passed (6.71 s); tags, conflicts, verdicts, draft
parity, replacements and lead text all exercised before implementation. Initial
green: 44 passed (5.54 s). Added namespace collision regression: 1 failed before
the rescind exemption. Added policy inheritance regression: 1 failed, 1 passed
before inheritance. Existing assertions were preserved.

Final Python 3.10 command: `python -m pytest tests/test_work_tags.py
tests/test_reply_draft_delivery.py tests/test_skill_lint.py tests/test_install_skills.py
tests/test_owed_action_detection.py::test_exact_key_supersession_closes_only_named_generation_and_malformed_blocks
tests/test_owed_action_detection.py::test_requester_rescind_blocks_landed_response_proof
-q -p no:cacheprovider --basetemp <scratch>`: **144 passed in 15.23 s**.
Separate legacy CLI selection: `tests/test_cli.py -k 'task or reply'` with the
same options: **33 passed, 239 deselected in 13.29 s**. Python 3.14 control:
`tests/test_work_tags.py` with the same options: **57 passed in 7.15 s**.
Ruff and Bandit pass all changed Python modules and tests (B101 excluded for
pytest assertions); existing nosec-comment warnings are not findings.
Whitespace and privacy checks accompany the local commit. Task scratch
`tk-56e02d050a1d` retains red/green and compatibility logs for the cold read.
Commits remain local; no push or PR in this slice.

## B2a-f — finalized declaration and publisher ownership contract

Base `b54c05cc`; design `09b54296` section 8. Strict boolean/true/false external
declarations inherit across replacements and replies; contradictory overrides
refuse. True openers require current sole lead, review stage, item/full head,
repo/branch/target and explicit checks. Authority is rechecked under publication
lock. Declaration bounds: repository strings 256 characters, check JSON 4096
characters / 64 keys of at most 24 characters, empty-check reason 1024 characters.
Nonempty checks forbid a no-gates reason. Both vendor metadata spellings refuse
at shared publication, including injected draft metadata. B2b owns map generation.
Both skill twins revise the existing cancellation, external provenance, exact
vendor-map and B6a/B6b themes; all-cycle independence remains. Whole-history mixed
provenance stays B3 work; no rescind, cleanup or reducer implementation is added.
Failing-first: 18 failed, 57 deselected (2.51 s); first green: 75 passed (8.77 s).
Final Python 3.10 tags/drafts/skill lint/install selection: 164 passed (13.47 s).
Python 3.14 work-tag control: 81 passed (8.93 s). Ruff and Bandit pass changed
Python files (B101 excluded for test assertions); whitespace check passes.
Targeted foreground logs retained under task scratch `tk-b3b13b81a46b` for review.

## B2b — operator vendor map and escalation links

Contributor/operator record, base `f0e6ea28`, design `09b54296` section 2. Configure
an active seat with `agenttalk roster set-model-vendor <agent> alibaba`; clear with
`agenttalk roster set-model-vendor <agent> --clear`. Config uses `model_vendor`;
closed values are anthropic/openai/alibaba/other/unverified, absent = unverified.
This is an operator assertion, never transport inference or provider attestation.
For example qwen through the Claude CLI remains alibaba. Rename carries config;
removal/retirement clears config while historical dispatch maps remain unchanged.

Task/review-request publication stamps reserved `assignee_model_vendors`. Group
fan-out freezes one exact-recipient map and request ID; resume reuses it and refuses
conflicting maps. Generic metadata and draft spoofing stay refused. Interpretation:
the existing broadcast command now accepts task/review-request to provide group
work dispatch; tasks retain lead/liaison and old-reader refusal (`--force` override).
`agenttalk escalate --work-item <slug> --work-cycle <positive integer>` uses the
same validator as metadata. Attention refs expose the exact escalation request ID
plus validated item/cycle (legacy cycle defaults 1); malformed/unlinked refs remain
team attention. Similar subjects do not merge distinct escalation IDs. No bodies
are added to the sanitized refs. Malformed external check JSON now shares the
wrong-shape refusal. No reducer/vendor-independence verdict is introduced here.

Failing-first: 15 failed, 4 passed (1.96 s); initial combined B2a/B2b green:
100 passed (11.69 s). Task scratch `tk-c127c9964399` retains logs for cold review.
Final Python 3.10 vendor/tags/attention suites: 320 passed (25.40 s); existing
CLI/store/web compatibility selection: 79 passed, 599 deselected (29.69 s).
Python 3.14 vendor/tag control: 106 passed (12.81 s). Ruff passes all changed
Python files. Bandit passes production and new tests (B101 excluded for test
assertions); the existing web test file has 37 baseline findings, unchanged.

### B2b cold-read follow-up — task resume gates

Initial dispatch and resume share the task authority/reader checks. Resume uses
the frozen opener kind and current lead/liaison authority; old-reader overrides
require `--force` again. Frozen vendor maps remain unchanged. Both demotion and
old-reader regressions failed before the fix (2 failed, 24 deselected, 1.02 s).
Targeted vendor/broadcast checks pass on Python 3.10 and 3.14 (33 passed each,
265 deselected); Ruff and Bandit pass (B101 excluded for test assertions).
Task scratch `tk-aaa3fe3d0b17` retains red/green evidence for the delta read.

## B4a — shared per-root validated snapshot

Contributor record, base `f0e6ea28`, design `09b54296` section 5. The HTTP server
warms one snapshot per root and refreshes outside request handlers, coalesced to
one start per five seconds. Direct composition calls remain synchronous. The
existing store scanner supplies both paths; roster/signature validation is shared.
Membership/stat and config/key checks reject mid-scan changes; explicit invalidation
discards in-flight generations. After the cold-read correction below, failures
serve fresh prior data with degraded coverage instead of dropping the team view.
State keeps its output/error shapes and active-only semantics, with a 15-second
freshness ceiling. Four mutation-between-poll tests now advance the worker clock;
their payload assertions are unchanged. Worker cancellation checks precede reads.
The selected-closure helper deduplicates both supplied partitions and budgets only
selected IDs (50,000 / 128 MiB), reports 60% warnings and refuses partial results.
B3 selects/expands the closure. B4s will populate the body-free archive tuple and
complete-discovery flag; until then board coverage stays building. No archive
scanner, reducer, new HTTP feed, retention change or persistent index is added.
Failing-first: eight new contracts failed before implementation, then eight passed.
Task scratch `tk-90bbd471392f` retains logs for the cold read. The legacy 1,000-send
performance fixture was interrupted during setup; cap tests use small injected limits.
Final Python 3.10 snapshot/state selection: 58 passed, 190 deselected (30.75 s);
compaction: 16 passed (3.46 s). Python 3.14 snapshot: 10 passed (2.14 s).
Ruff and Bandit pass changed modules; whitespace check passes. Commit stays local.

### B4a cold-read correction — last-known-good on a scan race

`active()` now prefers the published generation despite a refresh error, provided
its config matches and scan-start age is at most 15 seconds. Coverage remains
degraded until successful refresh. Without a usable generation it stays building
or refuses; config mismatch and staleness still fail closed. Membership races use
a dedicated exception and schedule a 250 ms retry that wakes the worker; repeated
races cannot busy-loop. Normal refreshes retain the five-second cadence.
HTTP regressions publish a real message both before and after the scanner read,
and assert the entire root key set and counts survive until recovery. The worker
retry test also verifies automatic execution without waiting for the normal poll.
Failing-first: 3 failed / 9 deselected, including both HTTP reproductions (1.45 s).
Task scratch `tk-bb35ff68d3a9` retains the red/green and final targeted logs.
Final Python 3.10 snapshot/state selection: 61 passed, 190 deselected (31.76 s).
Python 3.14 snapshot tests: 13 passed (2.66 s). Ruff, Bandit, whitespace and
added-line privacy checks pass. No full suite; commit remains local.

## B4s — complete compacted discovery and fingerprint cache

Contributor record, base `f714881a`, design `09b54296` section 5. The same per-root
service now enumerates every envelope in `messages/` and `archived/compacted/`,
including compaction collision filenames; reset-session archives stay excluded.
The canonical scanner accepts changed-file paths without changing delivery/retention.
Identical canonical IDs dedup, preferring active; different contents remain a conflict.

Cache keys include path/file identity, size, nanosecond modification/change times,
and config/signing generation. Unchanged valid files avoid reads; invalid verdicts
are retried behind new/changed files to prevent starvation. Archive facts retain no
body/Message. Restart/invalidation forces validation. Fingerprints cannot detect an
actor preserving every stat while rewriting.

Each refresh reads at most 1,000 changed archive files or 250 ms of archive work,
then publishes active state independently. Remaining discovery resumes next refresh;
coverage stays building until complete, incomplete on invalid envelopes, stale on
archive failure. Membership races schedule the bounded prompt retry; HTTP never scans.
Counts include both partitions; closure caps/warnings count selected IDs only.
Old FIX replies, entirely archived items and legacy metadata ignore the UI window.
B3 owns selection/dependency expansion and legacy display; no reducer/endpoint is added.

Failing-first: 12 discovery contracts failed (1.69 s). A separate one-file-slice
starvation regression failed before its fix (0.36 s); empty-roster parity failed
before its fix (0.17 s). Final discovery/snapshot/compaction/scanner: Python 3.10
51 passed (8.98 s), Python 3.14 51 passed (8.87 s). Existing state/delivery selection:
61 passed, 345 deselected (46.43 s). Ruff/Bandit pass (B101 excluded for tests).
Task scratch `tk-3e4f33ca3f41` retains logs. Limits use small injected values;
no full suite or large-file population. Whitespace/privacy checks pass.

## B3a — pure causal reducer

Base: `feat/work-board-b2a` `b54c05c`. Contract: design/work-board `09b5429`,
sections 2, 3 and 6 and the B3a row in section 9. Branch: `feat/work-board-b3a`.

`work_board.reduce(messages, *, lead, incidents=(), integrated=None,
running=frozenset(), checks=None)` returns `{"items": [...], "legacy": {...}}`.
It is pure: no store, clock, filesystem or Git access, and the input is never
mutated. Facts the bus cannot prove are injected by later slices and default to
unknown: linked unresolved operator incidents (B2b/B7), candidate integration
(B5), exact fresh health `(agent, request_id)` and per-`(item, cycle)` check
results (B6a). Correctness uses explicit correlation only (request_id,
in_reply_to, supersedes, declared cycle); message IDs order nothing. Bodies and
subjects are never read; seat names are compared as identities only.
`threads._classify_event` supplies the existing participant/kind/status rules.

| Row | Placement | Test |
| --- | --- | --- |
| 1 | needs_you overlay; underlying placement kept | `test_row1_linked_incident_overlays_needs_you_and_keeps_placement` |
| 2 | Unknown: conflicting/missing history | `test_row2_conflicting_terminal_replies_are_unknown` |
| 3 | Done; merged with open FIX/HOLD | `test_row3_integrated_candidate_is_done_and_keeps_open_fix_visible` |
| 4 | Fix round (dispatched/accepted/running; unresolved FIX) | `test_row4_unresolved_fix_or_active_fix_task_is_fix_round` |
| 5 | Building (accepted or exact running; design badge) | `test_row5_accepted_or_running_build_is_building` |
| 6 | Independent review; unverified review | `test_row6_outstanding_review_labels_unproved_independence` |
| 7 | Ready (deliverable, one candidate, independent GO, policy) | `test_row7_ready_needs_deliverable_independent_go_and_check_policy` |
| 8 | Queued: start unconfirmed | `test_row8_dispatched_build_without_evidence_is_queued` |
| 9 | Unknown with the exact reason | `test_row9_unknown_carries_the_exact_reason` |

Cross-cutting tests: a later GO never hides a FIX under reversed writer clocks;
requester-only rescind and the earlier-requester blocker; a superseded FIX awaits
its replacement review; external deliverable and M3 mixed provenance; the one
counted legacy group; purity, input-order freedom and no body/subject reads.
Fixture envelopes copy live 2026-09-26 meta shapes (task openers with
epoch_at_send/request_id/stage/work_item; task-responses with
in_reply_to/request_id/status/verdict; review-results; rescind with request_id).

Interpretations, for the cold read:

- Title is `work_title` only. The subject fallback in section 2 is left to the
  display layer, because this slice reads no subjects.
- Row 1 is an overlay: `column` is needs_you while `workflow_column` keeps rows
  2-9. The incident input is already canonical and unresolved; Later/Wait
  deferral is client-local.
- The earlier-requester rule: rows 4-6 still report real activity. When an
  earlier requester's unaccepted execution is what remains, placement is Unknown
  with the exact design reason instead of Queued; it is never Ready.
- Row 2 carries history conflicts: ambiguous fan-out, multiple terminal replies,
  reply plus requester rescind, a reply contradicting its opener, an orphan
  tagged reply, malformed tags and M3. Row 9 takes the first applicable semantic
  reason in a fixed order.
- Candidate = the heads of surviving (not superseded, not rescinded) reviews
  plus an external declaration's head. More than one is "multiple candidates
  without supersession", never newest-wins.
- A FIX/HOLD resolves only through a GO on a direct `supersedes` replacement;
  an outstanding replacement reads "awaiting replacement review". Longer chains
  are B3b.
- Check policy comes from the current cycle's originating design/build (or
  external review) dispatch, else from earlier cycles, so a fix-only later cycle
  inherits it. `no_gates_reason` gives "local checks not tracked"; named gates
  need the injected check fact.
- Older cycles' outstanding obligations are diagnostics in `previous_cycles`,
  not blockers. A terminal design cycle followed by a lead-started cycle is
  noted "design phase ended by lead; operator approval unrecorded".
- `external_deliverable` accepts true/false only, as B2a-f will publish; other
  values are malformed (row 2). `assignee_model_vendors` is read per recipient;
  missing is unverified.
- Coverage staleness (B4a) and the highest-cycle authority check (B2a
  publication) are outside this pure function.

### Executed validation

All runs foreground with `PYTHONPATH=<worktree>/src`, `PYTHONDONTWRITEBYTECODE=1`,
`python -B`, task-scratch TEMP/TMP/basetemps and `-q -p no:cacheprovider`.

- Failing first, against a stub module: **14 failed, 1 passed**. Only the purity
  test passed, trivially.
- First implementation: 14 passed, 1 failed. The purity test caught a real
  input-order dependence in the obligation list; outputs are now sorted.
- A policy-origin fallback was then added with its test; that test failed
  without the fallback.
- Final: `tests/test_work_board_reducer.py` **15 passed** on Python 3.14.6.
  With `tests/test_work_tags.py`: **72 passed** on Python 3.10.11 and on 3.14.6.
- Mutation (committed first, restored through git, bytecode-free): **14/14
  killed**. Mutants covered policy fallback, a later GO hiding a FIX,
  any-sender rescind, the earlier-requester rule, ignored independence, the
  needs-you overlay, multiple terminal replies, Done without integration, M3,
  legacy counting closed work, supersede resolution, a missing verdict counted
  as success, a malformed external flag and output order.
- Ruff and Bandit (B101 excluded for pytest assertions) are clean for both files.
- Size: `work_board.py` 320 lines, tests 255, this record about 90. That is
  above the 350-500 estimate but below the 700-line split threshold.

## B3b — adversarial reducer fixtures and cold-read corrections

Base: B3a `222cb7d` merged with B2a-f `f0e6ea2` (merge `6f66de6`, documentation
conflicts only), so `external_deliverable` and the reserved
`assignee_model_vendors` map are the built contract. Contract: design/work-board
`09b5429` sections 2 and 3, the B3b row in section 9, the codex B3a cold read
(eight findings) and the lead's M3 ruling.

Fixtures now publish through the real shared normalizer: the test `Bus`
serves as the store that `work_tags.normalize` consults, so replies inherit
tags, verdicts canonicalize, `supersedes` and external declarations are
validated exactly as at publication. `raw=True` marks the only exceptions:
malformed or pre-B2a history that validated reads still return. The
publisher-owned vendor map is added after normalization, as B2b will.

| Finding | Correction | Test |
| --- | --- | --- |
| 1 needs-info HOLD discarded | Native needs-info is a HOLD kept as verdict evidence. Rescinding the review closes the obligation but not the HOLD; only a successful replacement discharges it. | `test_f1_rescinded_needs_info_hold_is_not_erased_by_an_unrelated_go`, `test_hold_discharged_only_by_a_successful_replacement` |
| 2 design author independent | Design purpose persists until a cycle has a build dispatch, so a linked fix keeps design authors in the builder set. An unlinked fix in a design cycle makes purpose unknown (blocker). | `test_f2_linked_design_fix_keeps_the_design_author_out_of_independent_review`, `test_unlinked_fix_in_a_design_cycle_leaves_purpose_unknown` |
| 3 missing superseded opener | The supersession graph reports a missing target as row 2 "missing referenced opener", with foreign-item, cross-cycle, branching and cyclic edges likewise. | `test_f3_snapshot_missing_the_superseded_opener_is_unknown`, `test_historical_branching_cyclic_or_cross_cycle_supersession_is_unknown` |
| 4 repository policy dropped | The policy is the whole declaration: repo, branch, target, gates and no-gates reason. Differing declared origins are "conflicting repository/check policies". | `test_f4_parallel_origins_with_different_repositories_conflict` |
| 5 superseded success reused | Only the surviving end of each explicit chain can satisfy an execution obligation. A declined parallel execution needs its own replacement. | `test_f5_superseded_success_does_not_satisfy_a_declined_replacement`, `test_declined_parallel_execution_needs_its_own_replacement` |
| 6 one-edge resolution | A FIX/HOLD resolves when the surviving end of its replacement chain is a complete GO. A pending end reads "awaiting replacement review". | `test_f6_replacement_chain_discharges_the_original_fix` |
| 7 head conflict too late | Incomparable heads of surviving, non-rescinded reviews are a row 2 conflict before any activity row. A rescinded review pins no candidate. | `test_f7_pending_review_of_a_second_head_is_a_candidate_conflict_first`, `test_rescinded_review_does_not_pin_a_competing_candidate` |
| 8 malformed numbers abort | All opener fields parse inside one guard. Malformed history makes only that item row 2 "malformed work metadata" with its opener IDs; other items reduce. | `test_f8_malformed_historical_numbers_are_a_per_item_unknown` |
| M3 ruling | An external declaration conflicts only with a build/fix in the SAME cycle. A later seat-fix cycle is a normal build, still judged by the all-cycle builder set. | `test_external_deliverable_review_only_item_and_same_cycle_mixed_provenance`, `test_lead_ruling_m3_later_seat_fix_cycle_is_a_normal_build` |

Also corrected here: row 3 now says **integrated without independent GO**
when a merge bypassed independent review (design row 3: missing review evidence
stays explicit); every item carries a `checks` key.

Adversarial cases beyond the cold read:

- Every ordering and writer-clock skew: 40 seeded shuffles per scenario give
  identical output, and envelope IDs permuted with their links give identical
  placements. Scenarios: ready, FIX, chain, fan-out, rescinded HOLD.
- Fan-out: a group review needs every recipient's GO, and one FIX wins. A group
  build needs every recipient.
- Rescind/supersede interleavings: superseding is not cancelling; a rescinded
  replacement revives nothing; a withdrawn replacement review returns the FIX to
  a fix round.
- Partial snapshots: a missing opener is row 2, and a missing reply is Queued,
  never Ready.
- Pre-B2a history: a done reply without verdict or `verdict_issue` is "verdict
  missing", and a text `"false"` declaration is not external.

Interpretations, for the delta read:

- needs-info counts as a HOLD whatever its verdict field says. A later
  terminal reply of the same obligation governs, because a closed thread cannot
  precede its own needs-info.
- Design purpose is judged per current cycle: no build dispatch keeps design
  authors among builders. This is deliberately conservative for linked and
  unlinked fixes alike.
- Only declared policy tuples are compared. An origin that declares nothing is
  not a conflict.

### Executed validation

All runs foreground with `PYTHONPATH=<worktree>/src`, `PYTHONDONTWRITEBYTECODE=1`,
`python -B`, task-scratch TEMP/TMP/basetemps and `-q -p no:cacheprovider`.

- The merge alone: reducer and work-tag tests **96 passed**. All 15 B3a tests
  passed unchanged under real normalization.
- Failing first (reviewer sequences, M3 ruling, new adversarial cases):
  **11 failed, 19 passed**. The passing four (ordering/skew, both fan-outs,
  partial snapshots) already held on B3a and remain as regression guards.
- After the rewrite: **30 passed**. Four behaviour pins were then added after
  implementation, with mutation proving they are load-bearing.
- Two mutants survived and exposed real coverage gaps: B2a always stamps
  `verdict_issue`, and text `"false"` history. Both are now pinned.
- Final: `tests/test_work_board_reducer.py` **35 passed**. With
  `tests/test_work_tags.py`: **116 passed** on Python 3.10.11. With
  `tests/test_threads.py` too: **190 passed** on Python 3.14.6.
- Mutation (committed first, restored through git, bytecode-free): **28/28
  killed**. That covers the sixteen B3b corrections and edge guards plus the
  twelve re-anchored B3a rules. The mutation script refuses to run on an
  uncommitted tree.
- Ruff and Bandit (B101 excluded) are clean; the deterministic test RNG is
  annotated. `git diff --check` is clean.
- Size (B3b only, over merge `6f66de6`): `work_board.py` +216/-116, tests
  +331/-14, and this record. That is above the section 9 400-600 estimate
  once the adversarial suite is counted, and below the split threshold for code.

### B3b fix round 2 — codex delta read N1-N6

Base `b6ed00d`. The delta read closed F2, F3 and F5-F8 and verified the M3
ruling; F1 and F4 stayed open in further paths, and four more defects were
reproduced. Each fix below has a failing-first test from the reviewer's exact
sequence. N1's test uses a real isolated `Store`, which accepts an approved/GO
and then a needs-info/HOLD on the same native review.

| Finding | Correction | Test |
| --- | --- | --- |
| N1 terminal erased a later/unordered needs-info | Publication does not order a result and a needs-info; only explicit `in_reply_to` ancestry across all messages does. A HOLD the result provably answers is history. A HOLD proven later stays open. Otherwise the item is row 2 "ambiguous response order". Every HOLD stays in verdict evidence, and evidence (builders, verdicts) is now built before placement, so row-2 cards keep it. | `test_n1_real_store_go_then_needs_info_hold_is_ambiguous`, `test_n1_proven_order_decides_between_needs_info_and_terminal` |
| N2 integration beat a policy conflict | Policy is evaluated once, before any row. Conflicting declarations join the row-2 conflicts ahead of Done. Every item carries the raw `integration` facts for its heads, even when Unknown. | `test_n2_policy_conflict_precedes_done_and_keeps_the_raw_integration_fact` |
| N3 partial fan-out looked complete | The publisher's frozen `assignee_model_vendors` map is the recipient set. A named recipient without a copy is row 2 "incomplete fan-out: frozen recipient map names a missing opener"; a copy outside the map is row 2 too. | `test_n3_frozen_recipient_map_exposes_a_missing_fan_out_copy` |
| N4 missing policy covered by another origin | Every origin must declare. The origins are the cycle's builds, else its designs, else its external review, taken from the latest cycle that has one. A silent origin is "check policy missing". Replacements inherit, as B2a publishes. | `test_n4_an_origin_without_policy_is_not_covered_by_another` |
| N5 check labels wrong or dropped on Done | The label follows the fact: green, failed or evidence unavailable. Done keeps it explicit ("integrated with failed required checks"; "integrated; check policy missing"). | `test_n5_failed_check_stays_explicit_including_on_done` (+ N4 test) |
| N6 malformed title aborted everything | `work_title` and the vendor map are parsed inside the one opener guard. Malformed history gives a per-item row 2 "malformed work metadata" with evidence. | `test_n6_malformed_historical_title_is_a_per_item_unknown` |

Executed (foreground, `PYTHONPATH=<worktree>/src`, `python -B`, no bytecode,
task-scratch TEMP/TMP/basetemps, `-q -p no:cacheprovider`):

- Failing first: **7 failed, 35 passed**. Every failure was the intended one;
  the real `Store` accepted both review results and the reducer said Ready.
- The first implementation left one failure. The real-Store row-2 card had no
  verdict evidence, because verdicts were built only during placement. They
  are now built before it.
- Final: `tests/test_work_board_reducer.py` **42 passed**. With
  `tests/test_work_tags.py`: **123 passed** on Python 3.10.11. With
  `tests/test_threads.py` too: **197 passed** on Python 3.14.6.
- Mutation (committed first, restored through git, bytecode-free): **42/42
  killed**, covering 14 round-2 mutants plus 28 re-anchored earlier ones.
  One first-draft mutant (`{} or ...`) was equivalent code and was rewritten
  (`{} and ...`), then killed.
- Ruff and Bandit (B101 excluded) are clean; `git diff --check` is clean.
- Size over `b6ed00d`: `work_board.py` +104/-51, tests +119.

### B3b fix round 3 — codex delta read 2 N7-N9

Base `51ef3e8`. Closed by the delta read: N1, N3, N5 and N6. N2 and N4 stayed
partly open through N7. Each fix has a failing-first test from the reviewer's
exact sequence; N8's test is a real isolated `Store` round trip.

| Finding | Correction | Test |
| --- | --- | --- |
| N7 builds-else-designs hid a design origin (regression from round 2) | Every independent origin of the cycle is compared: design, build and external review, all non-superseding. Each must declare, and all declarations must agree. A design's conflicting or missing policy is no longer covered by a build. A policy conflict is ordered after more specific row-2 conflicts (such as M3), and every conflict reason is listed in `issues`. | `test_n7_every_design_and_build_origin_declares_one_policy` (with and without integration) |
| N8 direct-anchor-only reply grouping | A reply reaches its opener by walking explicit `in_reply_to` ancestry through non-opener messages, with a cycle guard. A stated `request_id` that disagrees with the ancestry's opener is row 2. | `test_n8_real_store_transitive_reply_ancestry_finds_its_opener`, `test_n8_ancestry_disagreeing_with_request_id_is_a_conflict` |
| N9 malformed correlation aborted the board | `request_id`/`in_reply_to` types are checked before any lookup, for openers and replies alike. A tagged envelope with malformed correlation is a per-item row 2 "malformed correlation" with its ID; other items reduce. | `test_n9_malformed_historical_correlation_is_a_per_item_unknown` |

Executed (foreground, `PYTHONPATH=<worktree>/src`, `python -B`, no bytecode,
task-scratch TEMP/TMP/basetemps, `-q -p no:cacheprovider`):

- Failing first: **4 failed, 42 passed**. The ancestry-contradiction test was
  tightened to a transitive path (reply, then note, then another opener),
  because the direct case was already caught.
- After the fix, one earlier test changed its reported reason. The same-cycle
  M3 case now also carries a derivative policy conflict. The ordering rule
  above restores the specific M3 reason, and the policy conflict stays listed.
- Final: `tests/test_work_board_reducer.py` **46 passed**. With
  `tests/test_work_tags.py`: **127 passed** on Python 3.10.11. With
  `tests/test_threads.py` too: **201 passed** on Python 3.14.6.
- Mutation (committed first, restored through git, bytecode-free): **49/49
  killed**, covering 7 round-3 mutants plus the 42 earlier ones. Confirmed on
  two full runs.
- Ruff and Bandit (B101 excluded) are clean; `git diff --check` is clean.
- Size over `51ef3e8`: `work_board.py` +34/-18, tests +74.

### Reducer fix round 4 — structural read-side validation (N10-N12)

Base `fb924fc`. A fresh codex reader (developer-4) closed N7-N9 and found N10-N12.
All three belong to the class the previous rounds kept patching one case at a
time: historical or partial evidence that modern publication would have refused,
or that is incomplete. This round replaces point fixes with one mechanism.

**Mechanism.**

- Before reduction, `_audit` applies the publication invariants to every envelope,
  historical or modern. These are `work_tags`' own functions, which `normalize`
  now calls too (`reply_request`, `reply_opener`, `inherit`, `reply_verdict`,
  `value`), plus `gates.validate_response_status`. The reducer keeps no forked
  copy of a publication rule.
- A violating envelope is withheld from reduction and recorded against every
  item it can still be attributed to: its request, its reply ancestry, and its
  own valid tag. That item becomes row-2 Unknown with the envelope as evidence;
  other items reduce normally.
- Any explicit reference (`request_id`, `in_reply_to`, `supersedes`) that does
  not resolve in the snapshot is "missing required correlation history". It is
  never a fallback. Cyclic ancestry and malformed correlation types are
  violations too.
- A request whose `work_item` is malformed is never demoted to legacy. It is
  attached through its explicit links (supersedes target, or correlated
  envelopes' tags); otherwise it is reported.
- Unattributable violations surface in a new top-level `unassigned`
  {count, reasons, examples} contract instead of disappearing.
- Uninterpretable verdict vocabulary is conflicting evidence (row 2).
- Done stays qualified when review or deliverable evidence is missing:
  "integrated with review outstanding", "integrated without a recorded
  deliverable".

| Finding | Closed by construction | Test |
| --- | --- | --- |
| N10 unresolved or cyclic link fell back to request_id | A named reference that does not resolve is missing history; cyclic ancestry is a violation. | `test_n10_removed_link_or_cyclic_ancestry_is_missing_history_not_a_fallback` |
| N11 historical rejected/GO counted as approval | The shared `reply_verdict` pairing runs on history. | `test_n11_historical_native_status_verdict_disagreement_is_not_an_approval` |
| N12 malformed untagged reply vanished | The violation is attributed by request, ancestry and tag. | `test_n12_malformed_reply_without_repeated_tag_is_attributed_by_its_request` |
| contract | Unattributable corruption is reported. | `test_unattributable_corruption_is_reported_not_silently_dropped` |
| attribution | Dispatch-only metadata on a reply, dual attribution, and a malformed tag attached to its item are pinned. | `test_audit_attributes_every_way_and_keeps_malformed_tags_attached` |

**Property test** (`test_property_no_single_deletion_or_corruption_certifies_ready_or_done`):

- It covers all 12 valid history shapes: 3 execution variants (single, superseded
  with rescind, fan-out) × 4 review variants (plain, needs-info answered through
  a link, FIX then replacement GO, fan-out). Each shape runs with and without
  injected integration.
- Every single deletion is tried. So is every corruption of a relied-on field:
  `[]`, plus `"garbage"` for fields that have a domain, plus a stranger in the
  checked participants.
- 1,996 damaged histories: none is Ready, and none is a clean Done.
- Interpretation: design row 3 lets proven integration say Done only with its
  open or missing evidence stated, so a *qualified* Done under damage is
  correct.
- It found two gaps the targeted tests had missed, both fixed: a malformed tag
  demoted a replacement to legacy, and a pending fan-out review hid behind a
  clean Done.
- It also showed two cases were never evidence, so they are out of scope:
  - an opener's recipient on an obligation its requester rescinded (cancellation
    is by request);
  - non-roster names, which Store validation keeps out of the input.

Executed (foreground, `PYTHONPATH=<worktree>/src`, `python -B`, no bytecode,
task-scratch TEMP/TMP/basetemps, `-q -p no:cacheprovider`, every command well
under 9 minutes):

- Failing first: **5 failed**. These were N10 (removed link, cycle), N11, N12,
  the unassigned contract, and the property test, which failed at once on N10's
  fallback.
- The `work_tags` refactor preserved publication behaviour: `test_work_tags.py`
  plus `test_reply_draft_delivery.py` gave **124 passed** before the reducer
  changed.
- Final: `tests/test_work_board_reducer.py` **52 passed**.
  - With `test_work_tags.py`, `test_threads.py` and `test_reply_draft_delivery.py`:
    **250 passed** on Python 3.14.6.
  - The same set without threads: **176 passed** on Python 3.10.11.
  - `tests/test_cli.py -k "task or reply"`: **33 passed**.
- Mutation (committed first, restored through git, bytecode-free): **64/64
  killed**. That includes 19 round-4 mutants, 2 of them in the shared
  `work_tags` validators, plus every earlier rule re-anchored. The one first
  survivor (the no-deliverable Done qualification) became redundant inside the
  property once tag attribution landed; a direct review-only test now pins it.
- Ruff and Bandit (B101 excluded) are clean for `work_board.py`,
  `work_tags.py` and the tests; `git diff --check` is clean.
- Size over `fb924fc`: `work_board.py` +140/-69, `work_tags.py` +63/-26 (the
  extraction; behaviour unchanged), tests +177/-3.

### Reducer fix round 5 — audit completion, totality and the documented residual

Base `b55ee63`. Codex developer-4 closed N10-N12 and compared old and new
`normalize` over 4,632 inputs with no publication difference. It then found where
the audit did not yet reach (N13-N15). This round completes the mechanism.

**Completion.**

- Openers are audited too. The audit replays every publication invariant that is
  a function of the envelopes themselves, through two more extracted `work_tags`
  functions that `normalize` also calls:
  - `external_declaration`: the structure of an external declaration.
  - `replacement_target`: a replacement names an existing original of the same
    item and cycle.

  It also replays `inherit_dispatch`, so a replacement keeps its original's
  declaration and policy.
- A violation is recorded against every item any of its references reach:
  request, the whole reply ancestry, the replacement target and the envelope's
  own tag. Before, attribution stopped at the first.
- A tagged reply to an untagged opener is validated, as publication does, not
  waved through as legacy.
- `reduce()` is total. Each envelope's audit, each request, each item and the
  legacy group run in isolation. An internal fault makes only that item Unknown
  ("reducer could not evaluate this item (Type)"). Envelopes that are not
  well-formed objects are reported as `unassigned` "malformed envelope". A last
  resort returns an `error` instead of raising.
- Malformed-tag recovery uses only type-checked references and creates its item
  bucket safely (N14).

| Finding | Test |
| --- | --- |
| N13 attribution stopped at the first reference | `test_n13_violation_marks_every_item_its_references_reach` |
| N14 malformed-tag recovery aborted the board | `test_n14_malformed_tag_recovery_never_aborts_the_board` (3 variants) |
| N15 opener invariants not audited | `test_n15_opener_publication_invariants_are_audited_too` (repo removed; repo + checks removed) |
| totality | `test_reduce_is_total_an_internal_fault_isolates_one_item`, `test_every_isolation_layer_turns_a_fault_into_one_unknown_item`, `test_malformed_envelopes_are_reported_not_raised` |
| replacement and reply replay | `test_replayed_opener_invariants_reach_the_original_item`, `test_invalid_reply_status_is_evidence_not_silently_ignored` |

**Property tests.**

- **A** (round 4, kept): 1,996 single deletions or corruptions across the 12
  single-item shapes. None is Ready and none is a clean Done.
- **B** (new, `test_property_never_raises_and_rejected_history_never_certifies`)
  covers 15 shapes: the 12, plus two contradicting items, an item whose only
  opener carries the tag, and an external-deliverable item.
  - The damage space is every single deletion; every key set to `[]`,
    `"garbage"` or `7`, or removed; sender or recipient swapped to another
    roster seat or a stranger; and cross-item `in_reply_to`/`request_id` swaps.
  - Result: **5,700 damaged histories, and the reducer never raised on any.**
  - Realism is judged by replaying `work_tags.normalize` over each history, with
    dispatches first and then everything else in order.
  - **3,654 are rejected by publication, and none certifies any affected item.**
    The other 2,046 are histories publication would accept; the targeted tests
    judge those.
  - The oracle is order-canonical because, in an arbitrary fixture order, a fan-out
    reply anchored to a sibling copy only *looked* unpublishable. It is
    publishable once copies are dispatched first, and it still answers its own
    copy.

**Documented residual** (the lead's stopping rule). The reducer certifies only
from evidence in its snapshot. It is not a guarantee against the following:

1. **Historical authority and freshness.** It does not verify that a
   replacement's or external declaration's sender held lead, liaison or
   original-dispatch authority *at the time*, or that a replacement's request ID
   was fresh when published. Past roles are not recorded. Both checks run at
   publication only.
2. **Store validity.** Roster membership, signatures and envelope schema come
   from `valid_messages`. Non-roster identities never reach the reducer.
3. **Snapshot completeness.** A retained explicit reference to absent history
   makes the item Unknown. But a snapshot missing a *whole* thread with no
   retained reference to it looks exactly like work never dispatched. An example
   is a separate FIX review, request and reply both absent. The reducer then
   certifies from what remains. Complete discovery of both partitions before
   publishing a fresh generation is the B4a/B4s contract.
4. **Semantic truth.** Publication-valid history whose content is wrong in
   intent is outside any evidence reducer: a review that approves without
   reading, a mislabelled stage, a wrongly pinned head.
5. **Legacy.** Untagged openers only feed the counted Legacy group. Their replies
   are correlated but not validated, matching publication.
6. **Integration.** An injected merge fact (B5) may produce Done, but only with
   its missing or open review, deliverable or check evidence stated.
7. **Corruption.** Damage that publication would reject and the audit still
   misses is residual by rule. Property B bounds it over the enumerated space
   above.

Executed (foreground, `PYTHONPATH=<worktree>/src`, `python -B`, no bytecode,
task-scratch TEMP/TMP/basetemps, `-q -p no:cacheprovider`, each command well
under 9 minutes):

- Failing first: **5 failed**. These were N13, N14 (a KeyError), N15, totality
  (the injected fault escaped) and property B.
- Publication parity after both `work_tags` extractions: `test_work_tags.py`
  plus `test_reply_draft_delivery.py` gave **124 passed** each time.
- Final: `tests/test_work_board_reducer.py` **61 passed**.
  - With `test_work_tags.py`, `test_threads.py` and `test_reply_draft_delivery.py`:
    **259 passed** on Python 3.14.6.
  - The same set without threads: **185 passed** on Python 3.10.11.
  - `tests/test_cli.py -k "task or reply"`: **33 passed**.
- Mutation (committed first, restored through git, bytecode-free): **81/81
  killed**. That includes 17 round-5 mutants (2 in `work_tags`) and every
  earlier rule re-anchored. Two first-pass survivors were real coverage gaps,
  now pinned. An N14 variant's fixture short-circuited before the guard it was
  meant to exercise, and an invalid reply status was only ever ignored, never
  surfaced.
- Ruff and Bandit are clean; `git diff --check` is clean.
- Size over `b55ee63`: `work_board.py` +120/-53, `work_tags.py` +40/-16
  (extraction), tests +217.

### Reducer fix round 6 — N16, a qualified Done keeps every unresolved obligation

Base `ad7c5da`. Codex developer-4's final read, under the stopping rule, closed
N13-N15. It accepted the structural audit and the seven-point residual as
written. One realistic MAJOR remained. It violated residual point 6.

- **N16.** The sequence is fully publication-accepted, with dispatches first: a
  build with `no_gates_reason`; two independent reviews pinned to head A;
  build done/done; R1 done/GO; R2 done with no verdict (published with
  `verdict_issue` "verdict missing").
- Without integration the item was Unknown, "verdict missing" (correct). With
  the true merge fact it was a clean Done with no issues.
- The cause: row 3 asked only whether *some* independent GO existed and whether a
  review was still outstanding. The Ready blockers were never consulted.
- **Fix:** row 3 now reuses the same `_blockers` list that gates Ready.
  - The specific phrases stay: "merged with open FIX/HOLD", "integrated without
    independent GO", "integrated without a recorded deliverable", "integrated
    with review outstanding", "integrated with failed required checks".
  - Any other blocker yields "integrated; <blocker>", e.g. "integrated; verdict
    missing" or "integrated; declined review without replacement".
  - Every blocker reason joins `issues`, and its evidence joins `evidence`.
  - By construction, **a clean Done is exactly an integrated Ready**.
- Regression: `test_n16_integration_keeps_every_unresolved_obligation_explicit`.
  It covers the two-review missing-verdict sequence with and without
  integration, dev-4's two related controls (an unreplaced declined second
  review, a second builder with no verdict), and a pending second review.
- Property B now asserts that equivalence on every one of its 5,700 damaged
  histories. For each, the set of clean-Done items with the merge fact equals
  the set of Ready items without it. That was 715 matched pairs, with no
  mismatch. The never-raises and rejected-never-certifies counts are unchanged
  (5,700 / 3,654).

Executed (foreground, `PYTHONPATH=<worktree>/src`, `python -B`, no bytecode,
task-scratch TEMP/TMP/basetemps, `-q -p no:cacheprovider`, each command well
under 9 minutes):

- Failing first: **2 failed**. These were the N16 regression and property B,
  which found clean Done without Ready on damaged histories.
- Final: `tests/test_work_board_reducer.py` **62 passed**.
  - With `test_work_tags.py`, `test_threads.py` and `test_reply_draft_delivery.py`:
    **260 passed** on Python 3.14.6.
  - The same set without threads: **186 passed** on Python 3.10.11.
- Mutation (committed first, restored through git, bytecode-free): **83/83
  killed**, including 3 round-6 mutants. The one first-pass survivor was the
  now-redundant "integrated with review outstanding" phrase. It was still
  qualified through the generic path; a direct case now pins the clearer phrase.
- Ruff and Bandit are clean; `git diff --check` is clean.
- Size over `ad7c5da`: `work_board.py` +8/-6, tests +60/-12.
- The residual (round 5) is unchanged. Point 6 now holds by construction.
