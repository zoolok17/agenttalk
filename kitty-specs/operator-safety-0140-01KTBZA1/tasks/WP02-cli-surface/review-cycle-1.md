---
affected_files: []
cycle_number: 1
mission_slug: operator-safety-0140-01KTBZA1
reproduction_command:
reviewed_at: '2026-06-05T14:47:03Z'
reviewer_agent: unknown
verdict: rejected
wp_id: WP02
---

Findings:

- BLOCKING: `src/agenttalk/cli.py:702` resolves the composing recipient before `--to-request` is inspected, so `composing --from w1 --to-request q-1` fails in a normal multi-agent roster unless the caller also supplies `--to` or `AGENTTALK_PEER`. FR-016 / WP02 T014 require the sugar to mark composing against a specific request id with a single command argument. The request id already identifies the opener/responder pair, so the command should derive the peer from the thread row when `--to-request` is present, and only use/validate explicit `--to` as an optional consistency override. Repro smoke: in a `lead,w1,w2` store where `lead -> w1` opened `q-1`, `agenttalk --root <root> composing --from w1 --to-request q-1 --quiet` exits 2 with "no peer identity".

- BLOCKING: `src/agenttalk/cli.py:724` accepts any actionable thread for `composing --to-request`; that includes the requester's `open-outbound` view. WP02 T014 says the RID must be an open inbound thread for the sender. Repro smoke: after `alpha -> beta` opens question `q-1`, `agenttalk --root <root> composing --from alpha --to beta --to-request q-1 --quiet` exits 0, even though alpha is not drafting a reply and has no inbound obligation on that thread. Tighten validation to the inbound obligation state, e.g. `row.state == "owed-inbound"` and `row.role == "responder"` for the composing sender, then add tests for requester/outbound rejection and multi-agent single-argument peer inference.

Verification performed:

- Confirmed WP02's own delta over approved WP01 (`8c431267..860b2a6`) is limited to `src/agenttalk/cli.py` and `tests/test_cli.py`.
- `git diff --check 8c431267ca49319eda51db6b7ca53605e5fc1188..860b2a655733212d58fc1fac2761fecbd53344ac` passed.
- Verified `cmd_reply` / `_maybe_autogen_request_id` were not touched in the WP02 delta.
- Ran targeted tests: `PYTHONPATH=$PWD\src python -m pytest tests\test_cli.py -q` passed: 106 passed.
- Ran full suite: `PYTHONPATH=$PWD\src python -m pytest -q` passed: 482 passed, 2 skipped.
- Ran two throwaway CLI smoke tests proving the two composing issues above.

Residual risks:

- I did not find a blocking issue in the explicit no-heartbeat design call for `_reply_in_flight`; the marker-age-only rule is internally consistent with the documented rationale.
- Test runs emit Python 3.14 pytest-asyncio deprecation warnings and one Windows certificate-store warning; these are not caused by WP02.
