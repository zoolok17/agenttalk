# Areas that need permanent regression checks

Audience: contributors and reviewers deciding which checks a change needs.

**In plain words.** A small change in these areas can break several parts of the tool.
Past fixes sometimes protected one path while leaving another path exposed.
Keep a permanent test for the shared rule, and show that it detects a named, deliberately
introduced fault. This page names the existing checks and the gaps; it does not claim
that every listed area already has a complete class-wide test.

## The rule for a change

A PR touching a danger area adds or extends a permanent test shown red against a named
seeded fault, or explains in its description why that is not needed for this change.
Name the fault, the check that detects it, the observed failure, and the passing result
after restoring the correct behavior. A failed import, timeout or unrelated setup error
does not count as detecting the fault. Existing tests and independent review still apply.

The [development gate reference](DEV-GATE.md#permanent-capacity-regression-checks) gives
the commands for the account and expiry checks. Their fault tests temporarily replace
one decision in memory, assert that the normal behavior check fails for its named reason,
then restore the decision and repeat the passing check. They never edit production files.

## Areas and guards

| Area and shared rule | Key files | Past issues and changes | Permanent guard and remaining gap |
| --- | --- | --- | --- |
| Reply identity: every start path gives the child the identity of the seat it serves. | `src/agenttalk/wrapper/run.py`, `src/agenttalk/wrapper/prompt.py`, `src/agenttalk/cli.py` | #354, #403 | Class C: **next, after #403**. No class-wide start-path test in this change. |
| Usage readings: keep the source account and conversation attached to each figure; expired observations never become current because another part was refreshed. | `src/agenttalk/capacity.py`, `src/agenttalk/cli.py`, `src/agenttalk/checkpoint.py`, `src/agenttalk/web.py`, `src/agenttalk/wrapper/session.py` | #301, #391, #408 | `tests/test_capacity_account_class.py` covers publication, event recording, direct reading and eight named faults. `tests/test_capacity_expiry_class.py` covers five consumer families, including the CLI command and HTTP route, independent parts and ten freshness bypass cases. Known gap: `checkpoint.collect_context` borrows the caller's status line when saving for another seat (#408); a strict expected-failure test records that gap and must be updated when it is fixed. Existing parser tests remain in `tests/test_capacity.py`. |
| Deletion and links: only remove the intended owned tree; an unreadable path is not an absent path. | `src/agenttalk/janitor.py` | #342, #386, #399 | `tests/test_janitor.py`, including `test_342_ancestor_replaced_by_a_junction_after_discovery_is_kept_and_reported`. The ancestor-above-root gap is tracked in #399; the existing tests are not proof that it is closed. |
| Supervisor and gateway launches: success means the intended process started, with a typed failure when it did not. | `src/agenttalk/supervisor.py`, `src/agenttalk/powershell_host.py`, `src/agenttalk/ovh_gateway_service.py` | #123, #319 | `tests/test_supervisor_spawn_seam.py`, `tests/test_ovh_gateway_service.py`, `tests/test_gateway_port_guard.py`. Class-wide launch/failure mutation matrix: **none yet**. Tests must use owned processes and temporary data; live gateway ports remain excluded by default. |
| Persisted records: a field or reason change must agree with every reader and the complete saved outcome. | `src/agenttalk/store.py`, `src/agenttalk/wrapper/health.py`, `src/agenttalk/wrapper/session.py` | #305, #329 | `tests/test_stop_retries_off_golden.py` checks complete captured outcomes, including health records. A module's unit tests alone do not cover this boundary. Whole-format migration fault matrix: **none yet**. |
| Skills and documentation: installed instructions, supported commands and evidence fields must agree. | `src/agenttalk/skill_currency.py`, `src/agenttalk/install_skills.py`, `src/agenttalk/skills/` | No incident cited here. | `tests/test_skill_currency.py`, including `test_validator_backed_profile_rejects_each_dropped_bus_field`; `tests/test_install_skills.py` checks installed contents. Not every prose example is executed; add a literal-command check when changing an executable instruction. |
| Spend records: count each charge once and reject malformed stored money before conversion. | `src/agenttalk/ovh_gateway.py`, `src/agenttalk/ovh_gateway_service.py` | #309, #316 | `tests/test_ovh_gateway_report.py`, including `test_malformed_stored_money_refuses_the_report`; `tests/test_ovh_gateway_binding_readers.py`. Class-wide spend mutation matrix: **none yet**. These guards do not authorize a paid call or live ledger access. |

## Accepted boundaries of the new checks

The account tests use synthetic homes and distinct figures. They exercise the real
readers and temporary store, while replacing model execution and loop scheduling.
Shared provider homes also contain two conversations, so selecting the caller's thread
cannot pass unnoticed. A real directory link checks that a linked shared home stays shared.
An explicit status-line file may initialize an unbound seat; an existing mismatched binding
must be refused. Separate homes seeded from one login remain separate account labels (#401).
Two providers sharing one home must also remain separate. A direct `read_local` case
checks its account guard without a publisher checking first. Two rate-limit events pass
through `observe_event` before publication, so losing their account binding is detected.

The expiry tests hold publication still and move an injected clock from fresh, through
the 600-second boundary, to 601 seconds. They also retain a newer weekly reading while
dropping the old primary reading and refusal. They cover CLI text, status rows, attention,
web output and both checkpoint sources. The CLI command and an HTTP request to
`/api/state` are tested as well as their helpers. The checkpoint expiry fixture names the
calling seat; it does not claim that the other-seat gap in #408 is fixed.
They do not measure scan performance (#392), run
real model sessions, or replace CI's full platform and packaged-code checks.

## Regression log

Dates below identify the reported issue or review episode, not an inferred deployment date.
Only documented gaps are attributed to a particular kind of check.

| Date | Issue | Area | Which kind of check missed it |
| --- | --- | --- | --- |
| 2026-10-03 | #301 | Usage readings | Parser examples did not establish that live source selection and observation age were correct. |
| 2026-10-04 | #329 | Persisted records | The targeted unit-test selection omitted the complete saved-outcome checks; those checks later failed in CI. |
| 2026-10-05 | #342 | Deletion and links | A successful ordinary deletion did not exercise the stubborn-delete fallback through a linked folder. |
| 2026-10-05 | #354 | Reply identity | Checks of selected wrapper paths did not establish that every child received its seat identity. Class C remains next after #403. |
| 2026-10-07–08 | #391 | Account and expiry rules | Passing checks of individual fixes did not cover the same rule across manual, wrapped, relocated-home and consumer paths. The new class tests retain that cross-path coverage. |
| 2026-10-08 | #408 | Checkpoint context | Publication tests did not cover a separate reader of the caller's status line. The strict expected-failure case records the open other-seat gap; its production fix is separate. |
