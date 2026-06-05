---
affected_files: []
cycle_number: 1
mission_slug: team-scope-0150-01KTC934
reproduction_command:
reviewed_at: '2026-06-05T16:46:19Z'
reviewer_agent: unknown
verdict: rejected
wp_id: WP01
---

# WP01 Review Feedback — rejected

## Blocking finding

1. `quarantine_invalid()` can move a valid message file when an invalid message's embedded id collides with another file's stem.

   `list_invalid_messages()` reports only `(ident, reason)`, where `ident` may be the embedded message id for schema/validation failures. `list_invalid_message_paths()` then builds one global `ident_to_path` map using both filename stems and embedded ids via `setdefault` (`src/agenttalk/store.py:979`-`986`). If a valid file named `aaa.json` exists and a different invalid file `zzz.json` contains embedded id `aaa`, the map can resolve the invalid report id `aaa` to `aaa.json`. `quarantine_invalid()` then moves the valid file (`src/agenttalk/store.py:1007`-`1021`) and leaves the invalid file in `messages/`.

   Reproduction against head `a9879755093bc0e489839f0cf074d2e881d33122`:
   - Create valid `messages/aaa.json` with id `aaa` and kind `message`.
   - Create invalid `messages/zzz.json` with embedded id `aaa` and kind `not-a-kind`.
   - `list_invalid_messages()` returns `[('aaa', "unknown kind ...")]`.
   - `list_invalid_message_paths()` resolves that report to `aaa.json`.
   - `quarantine_invalid()` moves `aaa.json`; `zzz.json` remains in `messages/`.

   This violates WP01 T002's guarantee that valid files are never touched and that quarantine selection is the same validation gate used by status/doctor (`WP01-engine.md:60`-`67`). It also violates FR-010/FR-011's prune-selection contract (`spec.md:116`-`117`) and D5's move-only/same-gates safety requirement (`research.md:62`-`75`).

   Expected fix: make the invalid report carry enough file identity to resolve the exact failing path, or have the shared gate helper return both invalid report and source path in one pass. Add a regression test for stem/embedded-id collision where the invalid file's embedded id equals another valid file's stem; quarantine must move the invalid file only and leave the valid file byte-identical.

## Verification performed

- Resolved Spec Kitty review context for `team-scope-0150-01KTC934` / `WP01`; lane is `for_review`, head is `a9879755093bc0e489839f0cf074d2e881d33122`.
- Verified changed files are within WP01 owned files: `src/agenttalk/store.py`, `src/agenttalk/threads.py`, `tests/test_store.py`, `tests/test_threads.py`, `tests/test_teams.py`.
- `git diff --check 1da2286..a987975` passed.
- Targeted reproduction script confirmed the valid-file move bug above.
- `pytest tests/test_store.py tests/test_threads.py tests/test_teams.py -q`: 167 passed.
- `pytest -q`: 520 passed, 2 skipped.
- `ruff check .`: all checks passed.
