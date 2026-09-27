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
