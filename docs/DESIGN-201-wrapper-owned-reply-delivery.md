# DESIGN #201 — Wrapper-owned reply delivery (rev 2, post adversarial review)

Status: rev 2 — restructured after adversarial design review (approve-with-changes,
3 blockers). Author: claude-agenttalk-lead · 2026-08-21.
Field basis: the dogfood migration's dry-run retrospective (the dogfood target
repo's `docs/dryrun/RETRO.md`, finding 1).

## Problem

A wrapped child delivers replies by running an `agenttalk reply` subprocess in
its own shell. That channel fails as a class: the claude-wrapped dogfood seat
could not deliver a bus reply in 5 of 5 work turns (static command validation
+ unanswerable interactive approvals), the codex seat's first reply died on
missing `AGENTTALK_SELF`, and the claude adapter cannot even report that a
bus command was attempted (`TOOL_FINISHED` carries no tool name). Answers
dead-letter into files that are indistinguishable on the bus from a dead agent.

## Review verdict that reshaped rev 1 → rev 2

The rev 1 design put wrapper-owned delivery inside the commit-gate
(owed-action) path. The adversarial review verified against the dogfood
migration's ledger that **the commit gate was inactive for the entire dry run** (no
`POLICY_ENV` policy file → `PolicySnapshot.inactive` → zero obligations
admitted): every one of the motivating failures went through the FREEFORM
branch rev 1 had scoped out. Rev 1 also had three blocker defects in its
gate-path mechanics (broadcast digest fork, no completion seal, escalation
meta loss). Rev 2 therefore restructures:

- **PR-1 (this design, ship first): wrapper-owned reply delivery on the
  freeform path** — the branch every field fleet actually runs.
- **PR-2 (follow-up): the gate-path variant**, carrying ALL review findings
  (F1 broadcast echo parity in the intent — which also repairs today's
  latent dead broadcast recovery; F3 escalation meta enrichment
  `needs_operator=true` + subject; F4 step-6 CAS with fence revalidation;
  F5 both loop paths; F6 `mark_dispatch_result` side-effect parity +
  `wrapper_delivered` exemption from the unsatisfied-attempt flip;
  F10 ValueError semantics). Findings recorded verbatim in the review
  transcript; PR-2 must not start from rev 1.
- **PR-3: seat preflight echo-turn** (unchanged sketch: once-per-runtime-
  fingerprint probe through the injectable `agenttalk_preflight` seam +
  workspace write probe; failure → existing `config_blocked` hold).

## PR-1 design: freeform wrapper-owned delivery

### Core principle (review's central criticism, adopted)

No hand-derived parity. Every rule the CLI applies to a reply — correlation
echo, digest computation, typed-response validators — is extracted into one
shared module used by BOTH `cmd_reply` and the wrapper, so the two paths
cannot drift. New module `src/agenttalk/reply_transport.py`:

- `echo_reply_correlation(meta, *, anchor_id, anchor_meta, kind)` — the
  exact block now inlined in `cmd_reply` (in_reply_to; echo `request_id`
  unless the reply kind opens a new thread; echo `broadcast_id` only when no
  request_id — the F1 rule, one copy forever).
- `operation_digest_for(meta, *, operation, body, kind, recipient)` — the
  `_operation_idempotency` digest computation, one copy.
- `landed_reply_exists(store, *, agent, record) -> bool` — validated-message
  scan for sender=agent, recipient=record sender, `meta.in_reply_to` ==
  record id (the dedupe guard when a capable child used the CLI channel).
- `deliver_draft_reply(store, *, agent, record, draft_path) -> Message|None`
  — read the draft with `read_text(encoding="utf-8")` (F8: identical
  universal-newline semantics to the CLI `--file` path), refuse
  symlink/oversize(1 MiB)/empty; build meta via `echo_reply_correlation`;
  autogen request_id via the same helper `cmd_reply` uses; run
  `validate_response_status`/`validate_review_result_evidence` (no-ops for
  kind=message, kept for parity); mint a fresh 32-hex nonce; write the
  operation-intent marker; `store.send_operation(...)`. Any validation
  failure returns None — never a crash, never a partial publish.

`cmd_reply` is refactored to call the first two helpers (behavior-identical;
covered by a parity test asserting old inline logic == helper output on the
matrix of anchor shapes: tracked question, broadcast copy, untracked message,
counter-review kinds).

### Child contract (prompt change, freeform branch only)

The freeform branch of `assemble_turn_prompt` gains, when the record carries
a wrapper-declared `reply_draft`:

> PREFERRED — works in every sandbox: write your complete reply body to
> EXACTLY this file with your structured Write/Edit tool, then end your
> turn; the wrapper validates and delivers it on this thread. If your
> harness can run shell commands you may instead use the reply command
> below. Use ONE channel, never both.

The existing CLI instructions stay (capable seats keep a familiar path;
the wrapper's landed-check makes the channels race-free). The owed-action
branch is untouched in PR-1.

### Wrapper integration (both loop paths — review F5)

The loop (not `run.py`) decorates the record before `drive()`:
`record["reply_draft"] = {"path": <store.state_dir>/reply-drafts/<agent>/<record_id>.md}`
— deterministic, no new state threading; dir created lazily; the same
decoration in `_run_continuous`'s legacy (no-admission) region and in
`_run_one_shot`. Gate-admitted records are NOT decorated (they carry
`owed_action`; PR-2 territory).

After `outcome = _as_outcome(drive(record))`, in both paths:

1. **Completion seal (review F2):** proceed only when `outcome.ok` — i.e.
   turn finished, no watchdog kill, rc==0, no failure class. A truncated
   draft from a killed child is never published.
2. **Dedupe:** skip when `landed_reply_exists(...)` (child used the CLI
   channel this turn or a prior attempt already landed).
3. **Publish:** `deliver_draft_reply(...)`. On success, unlink the draft.
4. Fall through to the EXISTING landed-check/commit flow unchanged — the
   just-published reply is found by the same proof machinery
   (`resolve_landed_response`) that commits child-delivered work today, so
   commit/finalize logic does not change at all.
5. On any publish refusal: today's behavior, byte-for-byte (the turn is
   still ok; the child may simply have had nothing tracked to answer —
   freeform replies are not obligatory).

Draft files for failed/disposed turns are cleaned at dispose/dead-letter
time and are bounded by the existing per-message lifecycle (one draft per
inbound id, overwritten on redelivery).

### Crash windows (honest accounting)

- Crash after marker, before publish: the inbound record was never
  committed, so redelivery re-runs the turn (at-least-once, same as today);
  the orphaned `prepared` marker is inert (fresh nonce per attempt).
- Crash after publish, before commit: redelivery → the landed-check finds
  the published reply → commit without a paid turn (existing #73 machinery).
- Double-channel child: nonce dedupe cannot apply across channels (the CLI
  path has no nonce in freeform); the landed-check closes that window
  because the child's publication strictly precedes the wrapper's check.

### Composing signal (review F9)

Freeform turns never carried the composing ping, so PR-1 changes nothing
about composing. The gate-path composing question is PR-2's to answer
explicitly.

### Bounded residual (documented, not hidden; updated after the cold review)

- A seat that can neither run bus commands NOR write files cannot reply;
  PR-3's preflight exists to catch that seat before work is dispatched.
  (The draft dir lives under the store's `.agenttalk/state/`, i.e. inside
  the project root the wrapped child is normally granted — the dogfood
  claude seat's `--add-dir <root>` covers it — but PR-3's write probe must verify
  this per seat rather than assume it.)
- Typed multi-field responses (review-result, proposal-response, consult
  replies needing `consult=true`+`round`, NA responses needing
  `response=not-applicable`) are NOT carried by the draft channel in PR-1:
  consult-marked questions are excluded from decoration, typed threads were
  never decorated, and NA/broadcast-decline still needs the CLI. Typed
  kind+meta declaration through the draft is PR-2/fast-follow scope; the
  validators already run inside `deliver_draft_reply`'s refusal boundary so
  the extension inherits both them and the never-raise contract.
- A REFUSED draft (oversize/encoding/publish failure) on a committing turn
  is preserved as `<id>.refused.md` beside the draft dir — observable and
  operator-recoverable, but not yet surfaced in status/health; wiring a
  refusal signal into the wrapper lifecycle log is fast-follow scope.
- Draft-dir hygiene: delivered drafts are unlinked on publish, stale drafts
  are unlinked at next decoration, refused drafts are preserved by design;
  drafts for records disposed WITHOUT a later redelivery linger until then.
  No unbounded growth path beyond message volume; a reaper is out of scope.
- `deliver_draft_reply` does not call the request-id autogen helper: replies
  of kind=message never autogen (no prefix registered), so the CLI-parity
  claim holds by vacuity today — the PR-2 typed-kind extension must revisit.
- The freeform wrapper publish gives at-least-once with landed-check
  dedupe (in_reply_to OR same-thread request_id, bounded to messages after
  this inbound), not exactly-once across generations — matching the bus's
  existing delivery semantics.

## Tests (no model spend; review F7 test gaps folded in)

1. Parity: `echo_reply_correlation`/digest helpers vs. pre-refactor
   `cmd_reply` behavior across anchor matrix — INCLUDING a broadcast-copy
   anchor (request_id+broadcast_id both set → reply echoes request_id only).
2. Loop (fixture store + injected drive, `test_wrapper_loop.py` patterns):
   - clean turn + draft → exactly one bus message, exact correlation
     (in_reply_to, request_id), draft unlinked, record committed;
   - dirty outcome (watchdog kill / rc≠0) + partial draft → NO publish;
   - both channels (drive publishes via store like a CLI child, draft also
     present) → exactly one message;
   - no draft → behavior identical to today;
   - `_run_one_shot` variant of the happy path.
3. Stub canary (`tests/support/stub_cli.py` + `test_stub_agent_canary.py`):
   new scenario `draft_only` with the commit gate INACTIVE (the field
   configuration) — writes the declared draft via its structured-write
   path, runs no bus command; asserts the reply lands wrapper-delivered
   end-to-end through the real spawn path.
4. Prompt: freeform branch renders the draft instruction with the exact
   path; gate branch unchanged.

## Rollout

PR-1 changes child-visible instructions for every freeform turn (fleet-wide
blast radius — review R4): the CLI channel remains fully supported, so a
mixed fleet (old prompts cached in resumed sessions) is safe. Ship as a
minor release; dogfood-pattern smoke (one wrapped claude seat, one codex seat)
before tagging.
