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
