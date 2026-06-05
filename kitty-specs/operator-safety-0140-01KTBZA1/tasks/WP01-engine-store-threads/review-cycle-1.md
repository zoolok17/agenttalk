---
affected_files: []
cycle_number: 1
mission_slug: operator-safety-0140-01KTBZA1
reproduction_command:
reviewed_at: '2026-06-05T14:14:13Z'
reviewer_agent: unknown
verdict: rejected
wp_id: WP01
---

Findings:

- BLOCKING: src/agenttalk/threads.py:446 and src/agenttalk/threads.py:456 make any valid rescind override a per-agent manual close. WP01 T004 and data-model.md say `closed` and `closed-superseded` are both terminal, but the earlier deciding event wins; specifically, an earlier manual ack stays `closed` and existing closure paths are untouched. The new `test_rescind_outranks_manual_ack_for_reason` locks in the opposite behavior. Fix derivation so manual closure is preserved or compared against the rescind decision instead of always relabeling it as `closed-superseded`.

- BLOCKING: src/agenttalk/threads.py:465 marks an escalation as `operator_state="answered"` for any terminal state, including manual ack and supersession. FR-014/T005 define answered as the liaison sending a correlated non-control reply to the requester. A local manual ack should not fabricate an operator answer. Derive `answered` from the qualifying liaison reply event, or document and test a separate terminal-not-pending mapping before landing.

Verification performed:

- `git diff --name-status 76784bbf99d5e1ae9933d40c644deede0d152419..900363ef2877ebf4924e765ec42c313ab07a4151` matched WP01 owned files only.
- `git diff --check 76784bbf99d5e1ae9933d40c644deede0d152419..900363ef2877ebf4924e765ec42c313ab07a4151` passed.
- `PYTHONPATH=$PWD\src python -m pytest tests\test_store.py tests\test_threads.py -q` passed: 123 passed.
- `PYTHONPATH=$PWD\src python -m pytest -q` passed: 456 passed, 2 skipped.

Residual risks:

- I did not exercise CLI behavior because WP01 explicitly forbids cli.py changes; this review is scoped to store/thread/web/test foundations.
- Test runs emit Python 3.14 pytest-asyncio deprecation warnings and one Windows certificate-store warning; these are not caused by this WP.
