# Package lock order

Audience: maintainers changing publication, store or lifecycle operations.
Acquire locks in increasing rank. Release them before calling an earlier rank.
Skipping ranks is allowed. Never fix an inversion by retrying while retaining
the later lock. Store locks are scoped to one authoritative store; operations
must not hold locks from two different stores at once.

| Rank | Lock / acquisition sites |
| --- | --- |
| 1 | Process-local outer guards: wrapper heartbeat stamp/stop, runtime state, gateway permits, watchdog PowerShell cache. These protect callbacks into store/host operations; store transactions do not call back to acquire these guards. |
| 2 | Gateway lifecycle local-map guard (released before taking its per-path mutex). |
| 3 | Gateway lifecycle per-path thread mutex, then its cross-process OS lock (`lifecycle_lock`, `ovh_gateway_service`). |
| 10 | Comprehension `scan.lock` (`comprehension/lock`, scan pipeline/publish). No store path acquires this lock while holding a store lock. |
| 20 | Assurance `coverage.lock`, held over coverage execution. |
| 30 | Assurance `coverage-handoff.lock`, coordinates scan release and gate commit. It may remain held after rank 20 is released; it never reacquires rank 20. |
| 40 | `supervisor-lifecycle.lock`: supervisor, lifecycle helpers, CLI launch/refresh, store instance management. |
| 50 | `powershell-host.lock`: supervisor lifecycle and watchdog host selection. |
| 60 | `supervisor.instance.lock` file family (legacy instance coordination). |
| 70 | `locks/lane-reset.lock`: lane delivery/reset. |
| 80 | `locks/lane-*.transaction.lock`: delivery, resume and recovery. |
| 90 | `state/lane-*.cleanup.lock`: serialized lane cleanup. |
| 100 | `state/operation-publication.lock`: wrapper intent persistence and deduplicated send. |
| 110 | `.acceptance-write.lock`: all close writers, gate and knowledge writes, config transactions and administrative config/reset paths. |
| 120 | `closes/.*.lock`: per-close update. Nested successor creation is serialized by rank 110; same-ID reentry remains refused. |
| 130 | `config.lock`: roster, configuration, knowledge, gate CLI, lane state and wrapper policy/authority transactions. |
| 140 | Lane delivery `.worktree-integrity-secret.lock`: secret creation only; no callback into a store transaction. |
| 150 | Persistent retirement mutex: final send/requeue principal validation and remove/retire/rename/launch-request publication. |
| 160 | Persistent message-publication mutex: canonical bus write/order and wrapper replay. |
| 170 | `state/owed-action/ledger.lock`: obligation reducer/dispatch/recovery. |
| 180 | `state/owed-action/proof-health.lock`: proof health projection. |
| 190 | Per-agent lead-loop lease lock. Release may clear its waiting mirror. |
| 200 | Per-agent waiting lock. |
| 210 | Per-agent awaiting locks and other independent leaf store locks. No lower-rank callback is allowed. |
| 220 | Process-local message-ID lock: updates timestamp/random ID state only. |
| 230 | Ownership generation OS guards used by marker acquisition/release; and underlying OS lock primitives. No application callback runs inside a marker's generation guard. |
| 240 | Process-local diagnostic leaves: wrapper log stream, web rate/timeline state. They perform local state or output operations without store transaction callbacks. |

Equal-rank families are not a license to nest independent keys. Existing nested
close IDs are safe because rank 110 serializes all close writers; same-key
non-reentrancy is still enforced by the underlying mutex. The acceptance outer
lock alone has explicit same-process/thread/store reentrancy. The coverage
handoff's non-LIFO release is intentional and does not reverse acquisition.

`lock_order.hold` checks every `Store._exclusive_lock` plus both persistent
retirement/message mutexes before an OS wait. Unknown store lock paths are leaf
rank 210, so they cannot silently acquire acceptance/config afterward. A new
non-leaf lock requires classification and an inventory/test update. Internal
generation guards are implementation leaves, not independent application
transaction locks. Retirement and message publication reuse that OS primitive
as persistent application mutexes and are therefore checked at ranks 150/160,
not as marker-generation leaves. The gateway and scan protocols have independent ownership
implementations; their callers are outer operations, never entered by store
transaction callbacks. Process-local guards likewise do not create a reverse
store-to-outer acquisition edge.

Gate, knowledge and signoff paths have no additional mutex: they use ranks
110/120/130 as appropriate. Lane integrity uses rank 140. Bus sends under a
config transaction can omit retirement because config already excludes roster
mutation; sends otherwise use retirement then publication. Remove, retire,
rename and launch-request writes acquire config **before** retirement. This
preserves principal validation while allowing close publication to retain
acceptance and its ID lock through the release-barrier message.

The acquisition-site census covers `store`, `close`, `cli`, `assurance`,
`attention`, `checkpoint`, `gates`, `knowledge`, `lanes`, `lesson_context`,
`onboarding`, `recovery`, `supervisor`, `supervisor_lifecycle`,
`wrapper/obligations` and `wrapper/turn_watchdog`. Other primitives found by the
package scan are the gateway lifecycle, comprehension scan and process-local
guards listed above. Exclusive creation of an evidence/temp file is not a
mutex and is not assigned a transaction rank.

Contention is separate from lock order. Lane worktree add/remove still executes
inside config/acceptance ownership. Its Git timeout is normally 30 seconds plus
teardown, which can exceed a close writer's 10-second wait. Such a close returns
conflict without stealing locks; retry after the lane command completes. Do not
run lane maintenance concurrently with the final publication when a first-try
success is required. Moving Git outside that transaction would need generation
revalidation and is deferred. No throughput or bounded wall-time guarantee is
claimed.
