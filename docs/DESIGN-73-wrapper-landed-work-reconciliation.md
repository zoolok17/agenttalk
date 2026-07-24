# DESIGN — #73 wrapper landed-work reconciliation

**Status:** design and diagnosis only; one intentionally failing regression test,
no production change

**Audience:** wrapper, store, and owed-action maintainers reviewing the build
before implementation

**Author:** `codex-2` · 2026-07-24

**Base:** `origin/master` at `c70ee918c8e060191e125b12582dc9dcbcb23b18`

## Decision

Use the validated bus log as the authority for whether an inbound was answered.
Immediately after every legacy/no-admission drive, reconcile the exact inbound
through `DetectionCommitGate`. If a valid terminal response from the wrapped
agent to that inbound is durable, retain that evidence and perform the normal
guarded cursor/thread-seen finalization even when `DriveOutcome.ok` is false.
Record the heuristic disagreement for diagnostics, but do not park or escalate.

Also narrow the text classifiers. Structured command failure must be established
before failure-looking output is inspected, and gateway/raw-tail markers may
consume only lines rejected as non-JSON at ingestion time. These changes prevent
the known false classification, while bus reconciliation covers the larger class
of “write landed, child failed afterwards” races.

The invariant is:

> Never advance past an inbound that did no work; never park an inbound whose
> exact terminal work is already durable.

Here, “landed” means a validated, protocol-correct bus record. Assistant prose,
tool stdout, and a claimed successful command are never landing proof.

## Reproduced defect

### Production incident: 2026-07-24

The local bus provides a complete causal chain:

1. `20260724-111300-085374-PKXE` asked
   `codex-agenttalk-reviewer-1` for a review, request
   `rq-dc6ecf94b6ec`.
2. At `11:26:54.909730Z`, the reviewer durably published
   `20260724-112654-909654-Hjle`, a valid `review-result` with
   `status=rejected`, the same request id, and
   `in_reply_to=20260724-111300-085374-PKXE`.
3. At `11:27:26.649224Z`, 31.740 seconds later, the wrapper emitted
   `20260724-112726-649103-0lMi`: it said the same inbound was
   `config_blocked`, that the cursor had not advanced, and that the message was
   parked. Its quoted “error” was the successful `AGENTTALK :: REPLY` banner and
   named the already-published result id.
4. Health became `errored_ambiguous` with reason `config_blocked`. The reviewer
   cursor remained before the inbound. The lead manually routed the landed HOLD
   at `11:29:40`, requested a restart at `11:29:42.691597Z`, and the supervisor
   attempted a relaunch at `11:29:51Z`.

The reply body contained a finding referring to
`checkpoint.py:309-320 ... FileNotFoundError`. The loose interpreter regex is:

```text
(python(?:3)?|py).*(...|filenotfounderror|...)
```

It matched `py` at the end of `.py` and the later `FileNotFoundError`.
`classify_bus_execution()` therefore labeled an exit-zero, completed reply
`config_blocked/interpreter_not_found`.

A direct reproduction at the base SHA is:

```python
classify_bus_execution(
    "python -m agenttalk reply --to-request rq-demo --file result.md",
    "Exit code: 0\nHOLD\ncheckpoint.py:309-320 raised FileNotFoundError",
    0,
    {"item": {"exit_code": 0, "status": "completed", "success": True}},
)
```

It returns `kind=config_blocked`, `subtype=interpreter_not_found`. The explicit
success is available but consulted too late.

### Earlier instance: 2026-07-08

Inbound `20260708-204322-472607-vdkl` received review-result
`20260708-204648-718665-wz32`. Seven seconds later the wrapper parked the
inbound as `config_blocked`. A later, successful
`agenttalk escalate --help | Select-String ...` command emitted a benign
environment warning containing `Access is denied`; text classification again
ran before the exit-zero result was trusted. The operator sent an unnecessary
answer and created a replacement review request.

The July 24 result has the strongest possible exact `in_reply_to` proof. The
July 8 result is a valid same-request review-result but predates consistent
`in_reply_to` stamping, so it is strong operational evidence rather than a
candidate for the new failed-turn override.

## Current control flow

### Where the false failure is created

| Stage | Current behavior | Consequence |
| --- | --- | --- |
| `codex_adapter.py:41-60` | A parsed `command_execution` becomes `TOOL_FINISHED`; its `aggregated_output` includes the rendered reply body. | Valid assistant-authored review text reaches the tool classifier. |
| `run.py:1052-1119` | Runtime, interpreter, and execution-denied regexes scan output at lines 1066-1088. Structured exit/status failure is computed only at 1091-1092. | Exit-zero output can become `config_blocked`. |
| `run.py:2068-2075` | A required bus tool classified `config_blocked` sets `sig.config_blocked`. | The signal remains failed even after `TURN_FINISHED`. |
| `run.py:2129-2135` | `sig.ok` requires no config block and no bus failure. | A completed child becomes a non-ok `DriveOutcome`. |
| `run.py:1601-1703` | `config_blocked` has failure-class precedence. | The false signal becomes a sticky park class. |
| `loop.py:1004-1080` | The legacy continuous path commits only inside `if outcome.ok`; otherwise it records failure and immediately parks config-blocked. | Durable reply is ignored; cursor remains behind it. |
| `loop.py:1407-1440` | Scoped one-shot also trusts `outcome.ok` exclusively. | The request remains unseen despite a durable answer. |
| `health.py:187-193` | Config-blocked maps to health state `errored_ambiguous`. | The operator sees both labels for the same false negative. |

The active owed-action path already has the correct architectural order. It
drives at `loop.py:752`, replays the validated bus at line 756, then finalizes a
terminal response at lines 781-794 regardless of the child outcome. The gap is
the legacy/no-admission path used when policy is inactive and for kinds not
eligible for enforcement, including `review-request`, `proposal`, and ordinary
messages.

### Every path that can ignore already-landed work

Once an exact response is durable, the legacy paths can still return non-ok and
avoid commit when:

- a successful reply banner contains runtime, interpreter, or permission text;
- the bus append succeeds but the command exits nonzero afterwards;
- another required bus command later fails;
- the child exits nonzero or omits the recognized `TURN_FINISHED` boundary
  after publishing;
- a later adapter error, retryable error, watchdog, pipe/read exception, or
  terminal result occurs;
- a failed resume returns early after the resumed child published;
- an OVH raw-tail marker changes an already-failed turn to
  `config_blocked` or `infra`;
- terminal-text scanning mistakes quoted payload for a runtime diagnostic; or
- a previous attempt published the response, then the redelivered attempt hits
  preflight, spawn, setup, or configuration failure.

Accordingly, tightening one regex is necessary but insufficient. Any drive
failure class—`config_blocked`, `ambiguous`, `infra`, `poison`, or
`gateway_held`—can coexist with an already-landed terminal response.

## Raw-tail trust-boundary overlap

`_capture_child_output()` currently stores every child stdout line before JSON
parsing (`run.py:2028-2036`). `_ovh_qwen_failure_text()` appends every captured
line (`run.py:1591-1598`) and the OVH classifier scans that text first.

At the base SHA:

- a valid assistant JSON event containing `ATGW_POLICY_BLOCKED` is treated as a
  config diagnostic; and
- a valid tool-result JSON event mentioning `status code: 422` is treated as a
  gateway diagnostic.

This does not independently make a completed turn fail, but it relabels any
already-incomplete/nonzero turn as a sticky configuration park even when that
turn published its response.

This is the same P3 trust-boundary family cited around the PR #80 reviewer
incident: parsed assistant/tool JSON is payload, not discarded process
diagnostics. The existing non-JSON tail hardening originated in the PR #51
lineage. `_child_output_tail_text()` tries to exclude valid JSON for resume
attribution, but it reparses a byte-bounded tail. Long JSON can be left-truncated
by `normalize_child_output_tail()` and then appear invalid, so disposition must
be captured before truncation rather than inferred afterwards.

## Observed operator cost

The following figures are a local-store snapshot, not a population estimate.

- The store contains 262 operator dead-letter/escalation notices.
- Six are current wrapper `config_blocked` notices covering five distinct
  parked heads.
- Two of those five heads already had a durable review-result before the park:
  40% by distinct head, or two of six notices (33%). Both retained Codex
  reviewer config-blocked incidents were false parks.
- The three other distinct heads are Qwen budget/accounting/config holds with
  no observed landed response; those are genuine fail-closed parks.
- The July 24 false park cost a manual result relay, restart request, and
  supervisor relaunch attempt. The cursor and sticky health still remained
  wrong afterwards.
- The July 8 false park caused an unnecessary operator answer and replacement
  review request.

`config_blocked` is therefore the only repeatably and cleanly evidenced current
false-park class. Historical ambiguous/global-infra data is badly retry-weighted:
one already-answered old head generated 166 notices and 2,829 attempts under an
older runtime. It demonstrates the possible amplification cost, but must not be
mixed into a current false-positive rate.

## Options

### Option A — tighten the text heuristics only

Check exit status first, add word boundaries to the interpreter regex, and
exclude JSON payloads from raw-tail matching.

This is small and prevents both confirmed incidents. It does not cover a reply
that lands just before a child crash, post-send error, missing completion event,
or later tool failure. It leaves the wrapper trusting inference over durable
state. Reject as the complete fix.

### Option B — scan messages directly in `loop.py`

After any failed drive, call `Store.valid_messages()`, search for a plausible
response, and commit if one exists.

This covers post-send failures, but it would duplicate thread transition rules
and bypass `DetectionCommitGate` policy/fence/CAS retention. A subtly broad
request-id match could advance the wrong inbound. Reject.

### Option C — reconcile through `DetectionCommitGate` after every legacy drive

Extend the existing no-admission protocol with a post-drive reconciliation that
uses validated ordered replay, the exact inbound, the current wrapper fence,
the pinned policy generation, and the existing finalization path.

This reuses the component that already solves the same race for active owed
actions. It separates two questions cleanly:

1. Did the child process appear healthy?
2. Is the inbound’s terminal response durably present?

Choose Option C, together with Option A as defense in depth.

## Recommended mechanism

### 1. Add a post-drive no-admission reconciler

Refactor `record_no_admission_success()` into a method with semantics equivalent
to:

```text
reconcile_no_admission_after_drive(
    record,
    authorized_resolution,
    drive_succeeded,
) -> Resolution
```

Under the message-publication lock and owed-action ledger lock, it must:

1. Load publication-ordered validated messages. A validation/read error returns
   `BLOCKED` or `INDETERMINATE`; it never guesses progress.
2. Re-resolve the exact inbound and revalidate policy generation, wrapper fence,
   claim state, and scoped revision.
3. If replay finds a terminal response meeting the strict landed-work predicate,
   retain it with `_retain_same_generation_legacy_terminal_locked()` and return
   `no_admission_finalization_pending`, including the terminal evidence id.
   This branch is valid regardless of `drive_succeeded`.
4. If no terminal response exists and `drive_succeeded` is true, retain the
   current legacy success exactly as today.
5. If no terminal response exists and `drive_succeeded` is false, return a
   nonterminal resolution without marking success. The loop then follows its
   existing retry/park/dead-letter behavior.

This method must not accept “the tool said sent,” an exit-zero status, assistant
text, a draft file, or a Git commit as evidence.

### 2. Make the failed-turn override strict

Overriding a failed child outcome requires all of the following:

- the record is in `Store.valid_messages()` and publication order places it
  after the inbound;
- sender is exactly the wrapped agent;
- recipient is exactly the inbound sender;
- `meta.in_reply_to` equals the exact inbound id;
- request/broadcast correlation agrees when the inbound has one;
- kind, status, and transition are valid for the opener under existing thread
  protocol rules; and
- the response is terminal, not `composing`, a control record, a draft, or
  unrelated same-request chatter.

For an `outcome.ok` turn, current compatibility behavior may continue to accept
legacy request-correlated responses. A failed-turn override must require the
exact `in_reply_to` anchor. This keeps July 24 recoverable without letting an
old or sibling response advance the wrong head. Duplicate or conflicting
responses inherit the resolver’s existing nonterminal/indeterminate behavior;
only a terminal resolution advances.

An exact response from a previous crashed attempt also counts. That is desired:
on redelivery, the work is already complete and the wrapper should finalize
without invoking the model again.

### 3. Reconcile before recording failure

Both legacy call sites must use the same order:

```text
authorize legacy drive
record attempt start
drive
reconcile exact landed work
if terminal/finalization-pending:
    guarded finalize/commit
    clear attempt
    increment turns
    record heuristic disagreement telemetry
else if drive succeeded:
    existing success commit
else:
    existing failed-turn handling
```

For the continuous loop, finalization advances the global cursor through the
existing `_commit()`/`pre_commit` boundary. For scoped one-shot, it advances
only `thread_seen`, as today. The active owed-action path remains unchanged.

Retain terminal evidence before cursor projection. If lease/CAS/pre-commit
fails, the next poll retries only finalization and does not re-drive. A durable
response is immutable, so this closes the crash window between append and
cursor advance.

### 4. Preserve diagnostics without preserving the park

When landed work overrides a non-ok outcome, emit bounded structured telemetry:

```text
event=LANDED_WORK_OVERRIDES_DRIVE_FAILURE
inbound_id=<id>
evidence_id=<response id>
heuristic_class=<class>
heuristic_subtype=<subtype when known>
```

Do not persist an attempt failure, latch config-blocked health, or route an
operator escalation. The diagnostic is evidence that a classifier needs
attention, not evidence that the inbound is unfinished.

## Detector hardening

### Bus command execution

In `classify_bus_execution()`:

1. Parse explicit exit code/status/success first.
2. An explicit successful completion, especially exit code 0, cannot be
   `config_blocked` because of output text.
3. Only after a structured failed/unknown execution signal may runtime,
   interpreter, permission, argparse, or validation text refine the failure
   subtype.
4. Treat `--help` as non-writing even when the parsed verb is `reply`, `send`,
   or `escalate`.
5. Add token boundaries to interpreter markers as defense in depth, but do not
   rely on regex quality for the success contract.

If a CLI ever prints a semantic failure and exits zero, that is a separate CLI
contract defect. The wrapper must not infer command failure by scanning an
authored reply body.

### Child and raw tails

At ingestion, classify each line once as parsed JSON or discarded non-JSON.
Maintain:

- a bounded/redacted forensic tail for operator diagnostics; and
- a bounded/redacted diagnostic tail containing only discarded non-JSON lines.

`_ovh_qwen_failure_text()`, terminal marker scans, and resume-attribution
heuristics may consume only the diagnostic tail plus explicit adapter error
fields. They must never reparse a left-truncated forensic tail to decide whether
a line was JSON.

Apply the same ordering and tail rules to the cadence mirror so controller
health cannot be falsely degraded by rendered payload.

## Fail-closed matrix

| Durable exact terminal | Drive result | Required action |
| --- | --- | --- |
| Yes | ok | Finalize/commit normally. |
| Yes | failed by any heuristic class | Finalize/commit; suppress park/escalation; retain mismatch telemetry. |
| No | ok | Preserve current successful legacy commit behavior. |
| No | config/spawn/setup failure | Park; cursor/thread-seen unchanged. |
| No | ambiguous/infra/poison/gateway failure | Preserve current retry/disposition policy. |
| Unknown because bus validation/ledger/policy failed | any | Do not advance; report the gate failure through existing bounded mechanisms. |
| Same request but no exact `in_reply_to`, and drive failed | failed | Do not use as override evidence. |

## Intentionally failing regression

Added:

```text
tests/test_owed_action_detection.py::
test_same_policy_legacy_landed_terminal_overrides_false_config_blocked
```

The test uses a real temporary `Store` and the real `DetectionCommitGate` in its
same-policy legacy/no-admission mode. Its drive:

1. publishes an exact response from `beta` to `alpha` with the inbound request
   id and `in_reply_to`;
2. returns `DriveOutcome(ok=False, failure_class=config_blocked)`; and
3. lets the loop run exactly one poll.

The arrangement first proves the response is visible through validated store
reads. It then requires one completed turn, cursor advancement, a cleared
attempt record, no escalation, and a finalized `SATISFIED` gate claim naming the
response as terminal evidence.

At `c70ee91` it fails as intended:

```text
turns == 0            (expected 1)
cursor == ""          (expected inbound id)
attempt remains       (expected cleared)
config escalation     (expected none)
claim remains open    (expected finalized/SATISFIED)
```

This test deliberately injects the false classification instead of depending on
the current regex. Reordering the regex alone cannot make it pass; the landed
work invariant must be implemented.

## Build increments after approval

1. Commit the post-drive gate reconciliation and make the new regression pass
   in continuous mode.
2. Add scoped one-shot parity and crash/redelivery coverage.
3. Reorder structured command classification and update the existing tests that
   currently bless failure-looking text on successful command events.
4. Capture parse disposition at ingestion; route OVH, terminal, and resume
   classifiers to discarded-line diagnostics only.
5. Add valid assistant/tool JSON spoof cases, exit-zero reply-body cases,
   genuine nonzero spawn/config cases, duplicate/mismatched reply cases, and
   unreadable-ledger fail-closed cases.
6. Run the current hand gate, targeted wrapper/owed-action suites, and
   cross-platform Windows/POSIX CI before merge.

No production code is included in this design commit.
