# DESIGN — #73 wrapper landed-work reconciliation

**Status:** approved design implemented on the feature branch; pre-merge review
and operator inspection still required

**Audience:** wrapper, store, and owed-action maintainers reviewing the
implemented feature branch before merge

**Author:** `codex-2` · 2026-07-24

**Base:** `origin/master` at `c70ee918c8e060191e125b12582dc9dcbcb23b18`

## Decision

Use the validated bus log as the authority for whether an inbound was answered.
Add a policy-independent strict landed-response resolver shared by the loop and
owed-action code. Invoke it before re-driving a redelivered legacy inbound and
immediately after every legacy drive. If a valid terminal response from the
wrapped agent to that exact inbound is durable, perform the normal guarded
cursor/thread-seen commit even when `DriveOutcome.ok` is false. If a configured
commit-gate claim exists, retain the same evidence through that claim first; the
default inactive-policy path must not depend on a claim. Record the heuristic
disagreement for diagnostics, but do not park or escalate.

Also narrow the text classifiers. Structured command failure must be established
before failure-looking output is inspected, and gateway/raw-tail markers may
consume only lines rejected as non-JSON at ingestion time. These changes prevent
the known false classification, while bus reconciliation covers the larger class
of “write landed, child failed afterwards” races.

The park-side invariant is:

> A failed turn without resolved exact landed-work proof remains uncommitted;
> once validated replay proves an exact durable terminal response, that inbound
> is never parked.

Here, “landed” means a validated, protocol-correct bus record. Assistant prose,
tool stdout, and a claimed successful command are never landing proof.
Physical durability that cannot currently be validated is intentionally not
treated as proof: replay unavailability remains fail-closed until authority can
be recovered.

This change deliberately does not strengthen the pre-existing success path.
An exit-zero `DriveOutcome.ok` turn can still advance without a strict landed
bus response, as it could before #73. The CLI durability contract below makes
successful bus-write commands reliable evidence for that compatibility path,
but it is not equivalent to the failed-turn override.

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

## Base control flow before the fix

### Where the false failure is created

The following diagnosis describes the `c70ee91` base named above, not the
implemented feature branch.

| Stage | Base behavior | Consequence |
| --- | --- | --- |
| `codex_adapter.py:41-60` | A parsed `command_execution` becomes `TOOL_FINISHED`; its `aggregated_output` includes the rendered reply body. | Valid assistant-authored review text reaches the tool classifier. |
| `run.py:1052-1119` | Runtime, interpreter, and execution-denied regexes scan output at lines 1066-1088. Structured exit/status failure is computed only at 1091-1092. | Exit-zero output can become `config_blocked`. |
| `run.py:902-942, 1676-1690` | Terminal-text windows are scanned for bus/runtime denial markers without a landed-response check. | Quoted command/payload text can turn a terminal child result into config-blocked. |
| `run.py:1591-1656` | OVH failure text includes every captured tail line and gets classification precedence. | Parsed assistant/tool JSON can relabel an already-failed turn config/infra. |
| `run.py:2068-2075` | A required bus tool classified `config_blocked` sets `sig.config_blocked`. | The signal remains failed even after `TURN_FINISHED`. |
| `run.py:2129-2135` | `sig.ok` requires no config block and no bus failure. | A completed child becomes a non-ok `DriveOutcome`. |
| `run.py:1601-1703` | `config_blocked` has failure-class precedence. | The false signal becomes a sticky park class. |
| `loop.py:1004-1080` | The legacy continuous path commits only inside `if outcome.ok`; otherwise it records failure and immediately parks config-blocked. | Durable reply is ignored; cursor remains behind it. |
| `loop.py:1407-1440` | Scoped one-shot also trusts `outcome.ok` exclusively. | The request remains unseen despite a durable answer. |
| `run.py:2381-2456` | The cadence mirror uses the same bus-output classifier and completion predicates. | Rendered payload can falsely degrade controller health/backoff, although cadence does not own the inbound cursor. |
| `health.py:187-193` | Config-blocked maps to health state `errored_ambiguous`. | The operator sees both labels for the same false negative. |

The active owed-action path has the correct architectural order for admitted
questions. It drives at `loop.py:752`, replays the validated bus at line 756,
then finalizes a terminal response at lines 781-794 regardless of the child
outcome. It is precedent, not a reusable resolver as-is:

- `_resolve_replay()` requires `inbound.kind == "question"`
  (`obligations.py:2130-2148`) and calls
  `threads._classify_event("question", ...)` (`:2355-2357`);
- `review-request` and `proposal` therefore cannot be proven by that replay; and
- with no commit-gate policy configured, `admit_or_finalize()` returns the
  inactive `NOT_OWED` result without creating a no-admission claim or ledger
  revision. The confirmed July 24 inbound has no such claim.

The gap therefore covers the default wrapper configuration as well as
non-question opener kinds and ordinary messages.

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

At the base SHA, `_capture_child_output()` stores every child stdout line before
JSON parsing (`run.py:2028-2036`). `_ovh_qwen_failure_text()` appends every
captured line (`run.py:1591-1598`) and the OVH classifier scans that text first.

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
inside the loop and risks collapsing “validated replay unavailable” into “no
response.” A subtly broad request-id match could advance the wrong inbound.
Reject.

### Option C — shared strict bus resolver plus guarded disposition

Add one policy-independent resolver for exact landed responses. Use it from the
continuous and scoped legacy paths before drive and after drive. A configured
no-admission claim retains the proof through `DetectionCommitGate`; an inactive
policy uses the same guarded commit path as a normal successful legacy turn.

This reuses the normative thread transition rules without depending on the
question-only owed-action admission path. It separates two questions cleanly:

1. Did the child process appear healthy?
2. Is the inbound’s terminal response durably present?

Choose Option C, together with Option A as defense in depth.

## Recommended mechanism

### 1. Add a policy-independent exact landed-work resolver

Expose a resolver from the existing gate/replay boundary, for example
`DetectionCommitGate.resolve_exact_landed_terminal()`, with semantics equivalent
to:

```text
resolve_landed_work(store, agent, record)
    -> LandedWorkProof | NO_PROOF | PROOF_UNAVAILABLE
```

The gate object is always constructed by the wrapper, but this method must not
consult activation policy or require a claim; its authority is only validated
bus replay. A standalone helper used by the gate is equally acceptable.

From a consistent publication-ordered validated snapshot, using the store's
existing publication locking where required, it must:

1. Load publication-ordered validated messages. A validation/read error returns
   `PROOF_UNAVAILABLE`; it never guesses progress.
2. Locate the exact validated inbound by message id and verify its sender and
   recipient against the delivered record.
3. Inspect only later publication-ordered records for the strict predicate
   below.
4. For an opener in `OPENER_KINDS`, run the normative
   `threads._classify_event(inbound.kind, ...)` transitions with the real opener
   kind. Start with the ball on the responder, update it for needs-info
   ping-pong, and accept only a closing event whose terminal record has the
   exact inbound anchor.
5. Return the first valid terminal in publication order as an immutable proof
   containing at least inbound id, evidence id, evidence sequence, and response
   kind. Return `NO_PROOF` when no candidate closes the exact inbound.

The current question-only `_resolve_replay()` may delegate its terminal matching
to this shared helper after equivalence tests, but it cannot simply be reused
unchanged. The helper must not accept “the tool said sent,” an exit-zero status,
assistant text, a draft file, or a Git commit as evidence.

### 2. Make the failed-turn override strict

Overriding a failed child outcome requires an evidence record meeting all of the
following:

- the evidence is in `Store.valid_messages()` and publication order places it
  after the inbound;
- sender is exactly the wrapped agent;
- recipient is exactly the inbound sender;
- `meta.in_reply_to` equals the exact inbound id;
- request/broadcast correlation agrees when the inbound has one;
- for `review-request`, `proposal`, and `question`, kind, status, direction, and
  transition close the actual opener under existing thread protocol rules; and
- the response is terminal, not `composing`, a control record, a draft, or
  unrelated same-request chatter.

For a needs-info continuation, publication-order replay must first prove that
the delivered message handed the original review obligation back to this
agent; only an exact final verdict that then closes that opener counts.
Uncorrelated ordinary messages and no-reply notes have no protocol opener to
close, so they deliberately remain outside this override. This avoids treating
an acknowledgement as proof that unrelated file/Git work completed.

The proof inherits the bus trust model: when signing is enforced, the evidence
must pass HMAC validation; when signing is off, sender identity remains the same
auditable assertion used by all other bus consumers. The resolver does not
create a stronger authentication claim than `Store.valid_messages()`.

For an `outcome.ok` turn, current compatibility behavior may continue to accept
legacy request-correlated responses. A failed-turn override must require the
exact `in_reply_to` anchor. This keeps July 24 recoverable without letting an
old or sibling response advance the wrong head.

If more than one exact terminal exists, the first valid terminal in publication
order is the monotonic completion proof. This does not choose which verdict
content should win; normal bus/thread consumers still see every response. Later
terminals are duplicate/conflict telemetry and do not turn already completed
work back into “unfinished.”

An exact response from a previous crashed attempt also counts. That is desired:
on redelivery, the work is already complete and the wrapper should finalize
without invoking the model again.

This proof covers bus-deliverable terminal work only. A turn whose intended
deliverable is solely out of band—a Git push, a file write, or a note that owes
no response—has no terminal bus record for the resolver to prove and can still
false-park if child heuristics fail. Addressing that residual requires a
separate durable completion contract for those deliverables.

### 3. Reconcile before drive and before recording failure

Both legacy call sites must use the same order:

```text
resolve exact landed work from an earlier attempt
if proof:
    guarded commit; clear attempt; increment turns; do not drive
else if replay authority is unavailable:
    pause and retry without driving or advancing
authorize legacy drive
record attempt start
drive
reconcile exact landed work
if proof:
    if a configured no-admission claim exists:
        retain proof in the claim and finalize it
    else:
        use the normal guarded legacy commit
    clear attempt
    increment turns
    record heuristic disagreement telemetry
else if replay authority is unavailable and drive failed:
    preserve existing fail-closed failure handling
else if drive succeeded:
    existing success commit
else:
    existing failed-turn handling
```

For the continuous loop, disposition advances the global cursor through the
existing `_commit()`/`pre_commit` boundary. For scoped one-shot, it advances
only `thread_seen`, as today. Both count a pre-drive recovered response as a
completed turn so one-shot exits successfully.

When a configured legacy claim and revision exist, add a gate method that
revalidates and retains the supplied proof before cursor projection. When policy
is inactive, do not synthesize a policy claim merely to mark progress: the
validated bus record is already durable and `_commit()` supplies the wrapper
ownership/pre-commit guard. If lease/CAS/pre-commit fails, the next poll's
pre-drive resolver finds the same immutable response and retries disposition
without re-driving. This closes the crash window between append and cursor
advance without adding per-message ledger I/O to default wrappers.

Configured retention first repairs the canonical message index from the
validated publication stream, so a missed best-effort append hook cannot place a
claim transition before its evidence row. After validation, the exact evidence
identity is carried through finalization; turn accounting does not require a
second post-commit replay that could fail after cursor/thread disposition.

### 4. Preserve diagnostics without preserving the park

When landed work overrides a non-ok outcome, make a best-effort attempt to emit
bounded structured telemetry after disposition is already durable:

```text
event=LANDED_WORK_OVERRIDES_DRIVE_FAILURE
inbound_id=<id>
evidence_id=<response id>
heuristic_class=<class>
heuristic_summary_digest=<sha256>
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

The exit-zero assumption is load-bearing and is pinned by a CLI contract test:
`agenttalk send`, `reply`, and `escalate` all exit nonzero when the canonical
message-publication lock fails, and the validated bus remains unchanged. The
test injects the failure after preparation but before durable publication.
Classifier coverage includes Windows and POSIX pinned launch forms: direct
console scripts, unversioned or versioned `python -m`, the platform
`AGENTTALK_PY` environment syntax, and PowerShell/cmd/sh/bash command payloads
(including POSIX login-shell `-lc`).

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
| Unknown because pre-drive validated replay failed | not run | Do not drive or advance; pause and retry replay. |
| Unknown because post-drive validated replay failed | failed | Do not claim an override; preserve existing fail-closed failure handling. |
| Unknown because post-drive validated replay failed | ok | Preserve the pre-existing successful legacy commit behavior (the M1 residual). |
| Yes, but guarded cursor/thread-seen commit failed | any | Do not advance; retry proof and disposition before any re-drive. |
| Yes, configured claim retention/finalization failed | any | Do not advance; preserve the claim and retry its guarded finalization. |
| Same request but no exact `in_reply_to`, and drive failed | failed | Do not use as override evidence. |

## Failing-first regression

Added:

```text
tests/test_owed_action_detection.py::
test_inactive_policy_landed_review_result_overrides_false_config_blocked
```

The test uses a real temporary `Store` and the real `DetectionCommitGate` with
`AGENTTALK_COMMIT_GATE_POLICY` absent, matching the confirmed default production
path. Its inbound and response are the production protocol pair
`review-request` → `review-result(status=rejected)`. Its drive:

1. publishes that exact response from `beta` to `alpha` with the inbound request
   id and `in_reply_to`;
2. returns `DriveOutcome(ok=False, failure_class=config_blocked)`; and
3. lets the loop run exactly one poll.

The arrangement first proves the response is visible through validated store
reads. It then requires one completed turn, cursor advancement, a cleared
attempt record, and no escalation. It intentionally asserts only public wrapper
effects; the inactive policy creates no private no-admission claim.

At `c70ee91` it fails as intended:

```text
turns == 0            (expected 1)
cursor == ""          (expected inbound id)
attempt remains       (expected cleared)
config escalation     (expected none)
```

This test deliberately injects the false classification instead of depending on
the current regex. Reordering the regex alone could not make the initial test
pass; the implemented landed-work reconciliation is what turns it green.

## Implemented build increments

1. The strict resolver uses validated physical publication order and exact
   inbound anchoring, then applies the normative thread transition classifier.
2. Continuous and scoped loops reconcile before drive and immediately after
   drive, including configured-claim retention and crash recovery.
3. Command text is inspected only after structured failure; successful rendered
   reply bodies cannot trip configuration heuristics.
4. Ingestion records a separate discarded-non-JSON diagnostic tail. OVH
   raw-tail and resume marker scans never reparse the bounded forensic tail.
5. Tests cover inactive/configured paths, pre/post-drive recovery, wrong or
   nonterminal evidence, publication order, CLI non-durable writes, successful
   reply-body markers, and parsed-JSON gateway spoofing.
6. Configured retention repairs a missed eager projection before normalizing the
   terminal claim, and finalized turn accounting carries the validated evidence
   identity without a second replay.

Cross-platform CI and operator review remain merge gates.
