---
affected_files: []
cycle_number: 1
mission_slug: operator-safety-0140-01KTBZA1
reproduction_command:
reviewed_at: '2026-06-05T15:21:40Z'
reviewer_agent: unknown
verdict: rejected
wp_id: WP04
---

# WP04 review feedback

Status: rejected

## Blocking findings

1. T021 does not enforce strict additivity across the requested JSON surfaces, and one assertion directly normalizes the violation.

   WP04 T021 says to snapshot `status/sync/threads/whoami --json` for a 0.13.0-shaped flow and assert that every pre-existing key remains unchanged and that **no new keys appear** when no new features were used: "strict additivity: absent, not null". The submitted test only uses set equality on the `threads` row at `tests/test_coordination.py:389-391`; `sync` and `status` only get partial presence/absence checks, and `whoami` is allowed to emit the new field as null via `assert who["liaison"] is None` at `tests/test_coordination.py:402-403`.

   This leaves the release gate unable to catch exactly the "absent, not null" compatibility regression. Please make the no-feature compatibility test strict for all four requested JSON surfaces. If the stricter assertion exposes a product issue in a prior WP, keep WP04 test-only and report that owning-WP bug through the review loop.

2. The rescind wake-path gate does not assert the required user-visible rescinded output or final thread state.

   WP04 T019.1 requires the wake-path test to assert exit 3, output containing the `RESCINDED` banner plus the rescind reason, non-consuming behavior, and `threads --for exec` showing `closed-superseded`. `test_rescind_race_wake_path_mid_wait` currently checks the exit code and that the rescind remains unread (`tests/test_coordination.py:225-254`), but it never captures/asserts the waiter output and never checks the thread row. A wait implementation that returns exit 3 but omits the banner/reason or fails to surface `closed-superseded` could pass this release gate.

   Please capture the wait output from the thread and assert both `RESCINDED` and the reason text, then assert the exec thread row is `closed-superseded`.

3. The liaison refusal matrix checks exits but not the required remediation/error messages.

   FR-013 and NFR-004 require non-zero exit plus an actionable remediation hint for the new failure modes, and the WP04 reviewer guidance explicitly asks whether the refusal-matrix tests assert remediation hints rather than just exit codes. `test_liaison_flow_refusals_e2e` currently uses `_run_expect_exit(...)` for the no-liaison and self-escalation refusal paths (`tests/test_coordination.py:334-343`) without inspecting stderr/stdout.

   Please assert the actual refusal text for each error path that matters to FR-013/NFR-004, at minimum the no-liaison remediation (`set-operator-facing` / explicit `--to`) and the self-escalation refusal reason.

## Verified

- Changed files are limited to WP04 owned files: `tests/test_coordination.py`.
- `git diff --check 1e25f2b48bf0833769ccb7908887194880ce35fa..6977f8797982d6e0f748a7e8e1f8241f97815918` passed.
- `python -m pytest tests\test_coordination.py -q`: 24 passed.
- `python -m pytest -q`: 504 passed, 2 skipped.

The tests are green, but WP04 is a release-gate WP; green is insufficient if the gate omits the stated assertions.
