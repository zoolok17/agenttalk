---
affected_files: []
cycle_number: 1
mission_slug: team-scope-0150-01KTC934
reproduction_command:
reviewed_at: '2026-06-05T17:09:57Z'
reviewer_agent: unknown
verdict: rejected
wp_id: WP02
---

# WP02 Review Feedback — rejected

## Blocking findings

1. Partial-fanout remediation tells users to send a message that broadcast derivation ignores.

   Both the immediate exit-5 stderr and the persistent incomplete-batch warning tell the operator to re-send to missed members using only `--meta request_id=<bid>` (`src/agenttalk/cli.py:1183`-`1188`, `src/agenttalk/cli.py:542`-`546`). But the broadcast thread derivation only treats question copies with `meta.broadcast_id` as broadcast openers, and `cmd_broadcast` force-sets both `request_id` and `broadcast_id` plus frozen audience metadata on every fan-out copy (`src/agenttalk/cli.py:1157`-`1164`).

   Reproduction against head `a5c3fa414b39934dbaa37673ff117b219d6a2b6f`:
   - Create one delivered broadcast copy for `rev-a` with `request_id=b-demo`, `broadcast_id=b-demo`, `audience_resolved=rev-a,rev-b`, `batch_total=2`.
   - Status warns: `incomplete fan-out b-demo ... missed: rev-b. Re-send with --meta request_id=b-demo ...`.
   - Follow that instruction literally: `agenttalk send --from lead --to rev-b --kind question --meta request_id=b-demo -m x`.
   - Status still warns, and `threads` for `rev-b` has no row at all, because the resend lacks `broadcast_id` and is ignored by the broadcast derivation path.

   Impact: the documented recovery path does not recover the batch and can leave the missed member with no actionable thread, violating T007/T010's recovery/warning lifecycle. The fix can either make the printed remediation include the full required metadata/incantation, or add a CLI recovery path that can resend missed broadcast copies with the same bid and frozen broadcast metadata. Add a regression where following the printed/documented recovery clears the warning and gives the missed member an owed thread.

2. `reply --na` accepts explicit `--kind message`, despite the required mutual exclusion.

   WP02 T008 says `--na` is mutually exclusive with `--kind` and should force kind=`message` itself (`WP02-cli.md:73`-`75`). The implementation only rejects when `args.kind != "message"` (`src/agenttalk/cli.py:2149`-`2155`), so an explicit `--kind message --na` is accepted because `message` is also the argparse default.

   Reproduction:
   - Open a normal question thread `q1`.
   - Run `agenttalk reply --from beta --to-request q1 --na --kind message --quiet`.
   - Actual: exit 0, sends kind `message` with `meta.response=not-applicable`.
   - Expected: exit 2 conflict, same as any other explicit `--kind` with `--na`.

## Verification performed

- Resolved Spec Kitty review context for `team-scope-0150-01KTC934` / `WP02`; lane is `for_review`, head is `a5c3fa414b39934dbaa37673ff117b219d6a2b6f`.
- Verified changed files are within WP02 owned files: `src/agenttalk/cli.py`, `tests/test_cli.py`.
- `git diff --check 4160e7c..a5c3fa4` passed.
- Focused smokes reproduced both findings above.
- `pytest tests/test_cli.py -q`: 123 passed.
- `pytest -q`: 534 passed, 2 skipped.
- `ruff check .`: all checks passed.
