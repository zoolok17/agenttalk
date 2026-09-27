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
