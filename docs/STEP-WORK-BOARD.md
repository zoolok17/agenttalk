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
