---
description: Send a concrete proposal to a named agent over agenttalk and wait for an accept, reject, or counter response. Use when Claude should propose a specific design, plan, scope, or decision for agreement before proceeding.
reviewed-against: "0.105"
---

# /agenttalk.propose - Propose a concrete solution

You are running as a **Claude Code** agent. Use this command when you
need a named agent to decide on a specific proposal, not when you
merely need an open-ended answer.

`proposal` is for "I recommend this concrete solution; accept, reject,
or counter it." Use `question` for open questions, `review-request`
for already-implemented work, and `/agenttalk.consult` for
pressure-testing a draft answer to the user.

Proposals are point-to-point. If a whole group needs awareness, send a
follow-up `agenttalk broadcast --kind note`; do not treat a broadcast
as a multi-party acceptance protocol.

## Identity

Resolve your name and default peer in your current shell:

```powershell
$SELF = if ($env:AGENTTALK_SELF) { $env:AGENTTALK_SELF } else { "claude" }
$PEER = if ($env:AGENTTALK_PEER) { $env:AGENTTALK_PEER } else { "codex" }
```

`PEER` is the default decision target for the canonical two-agent
pair. In a larger roster, use `agenttalk roster` and choose a specific
agent such as `codex-lead`, `codex-rev`, or the named owner. Always
resolve inside your current shell - env from prior tool calls does
not persist across separate tool-call processes.

If `.agenttalk/` is not under the current directory, pass `--root
<path>` before the subcommand on every invocation, for example
`agenttalk --root <path> propose --from $SELF --to $PEER ...`. Do not
write `agenttalk propose --root ...`; global options must precede the
subcommand.

## Split-work guard

A proposal must not become a backdoor for splitting implementation
work. Unless the project's chosen planning authority already owns the
assignment, if the proposal assigns ownership of files/tasks between
agents, ask the user first. If the user already
approved the split, state the ownership boundary in the proposal or a
separate `kind=note`, and every implemented piece still needs a
`kind=review-request` cross-review before the work is called done.

This rule applies equally in a team roster: a proposal can lock the
plan, but it cannot silently make a named agent, role, or group own
implementation work.

## Procedure

1. Resolve `$SELF` and `$PEER`.
2. Choose `$TARGET`. Use `$PEER` for the default pair; otherwise pick
   the named decision-maker from `agenttalk roster`. If the user did
   not name a target and several agents could decide, ask.
3. Build the proposal body:
   ```text
   ## Problem
   <what needs a decision>

   ## Proposed solution
   <the concrete recommendation>

   ## Alternatives considered
   <credible alternatives and why they lose>

   ## Tradeoffs
   <risks, costs, constraints>

   ## Decision requested
   <accepted / rejected / countered, and what each means>

   ## Challenge
   <for major work (see /agenttalk.challenge): request id, verdict and
   disposition, or exempt:<reason>>
   ```
4. Send. Keep the `$TARGET` you chose in step 2; the default below applies only when none was chosen:
   ```powershell
   if (-not $TARGET) { $TARGET = if ($env:AGENTTALK_PEER) { $env:AGENTTALK_PEER } else { "codex" } }
   $reqId = "pp-$([guid]::NewGuid().ToString())"
   if ($env:AGENTTALK_WRAPPER_GENERATION) {
     # managed wrapper turn: record the across-turn wait, then stop and return to the wrapper
     agenttalk send --from $SELF --to $TARGET --kind proposal `
       --subject "<one-line decision>" --meta request_id=$reqId `
       --await-reply -m $body
   } else {
     agenttalk propose --from $SELF --to $TARGET `
       --subject "<one-line decision>" `
       --meta request_id=$reqId `
       -m $body
   }
   ```
   The `propose` command writes `kind=proposal`, auto-mints
   `meta.request_id=pp-...` if absent. Passing `$reqId` explicitly
   lets the wait below target the proposal thread.
   Inside a managed wrapper turn (`$env:AGENTTALK_WRAPPER_GENERATION` is set), `propose` records no
   across-turn wait. Send the proposal with `send --kind proposal --await-reply` instead and
   **return immediately to the wrapper**: it owns the inbox cursor and will deliver the correlated
   response in a later turn, so do not run `wait`, `sync`, `recv`, or `drain` from that child turn.
   `--await-reply` needs the tracked `request_id`. There is no `propose --await-reply`.
5. In a manual (unwrapped) session, wait for the response:
   ```powershell
   agenttalk wait --for $SELF --to-request $reqId --kind proposal-response --timeout 600
   ```
   The scoped wait ignores unrelated traffic and does not advance your
   global cursor.
6. Match the response by `meta.request_id`.
   - `proposal-response status=accepted`: proceed with the decision.
   - `proposal-response status=rejected`: do not proceed; either revise
     or ask the user.
   - `proposal-response status=countered`: treat the old proposal as
     closed, then read the fresh counter proposal linked by
     `meta.in_reply_to`.

In a manual session, scoped wait intentionally ignores unrelated traffic. If the proposal
stalls or you suspect another urgent thread is waiting on you, run
`agenttalk sync --for $SELF` and `agenttalk threads --for $SELF`.
Handle the older or more urgent thread first, then resume the scoped
wait for your proposal. Use `agenttalk reply --from $SELF --to-id <message_id>` or
`--to-request <request_id>` when replying inside a specific thread.

## Countering a proposal

To counter a proposal you received:

1. Close the old proposal. `--to-request` names the proposal you are countering; without it the
   reply goes to the newest message you received, which can be a different proposal:
   ```powershell
   agenttalk reply --from $SELF --to-request <old-request-id> --kind proposal-response `
     --meta status=countered `
     -m "<why a counter is needed>"
   ```
2. Send the counter as a fresh proposal with a NEW request id, linked to the old one. The same
   managed or manual choice as step 4 applies: `propose` records no across-turn wait, so inside a managed
   wrapper turn send it with `send --kind proposal --await-reply` and return immediately to the wrapper:
   ```powershell
   $counterId = "pp-$([guid]::NewGuid().ToString())"
   if ($env:AGENTTALK_WRAPPER_GENERATION) {
     # managed wrapper turn: record the across-turn wait, then stop and return to the wrapper
     agenttalk send --from $SELF --to $TARGET --kind proposal `
       --meta request_id=$counterId --meta in_reply_to=<old-request-id> `
       --subject "<counter proposal>" --await-reply -m $body
   } else {
     agenttalk propose --from $SELF --to $TARGET `
       --in-reply-to <old-request-id> --meta request_id=$counterId `
       --subject "<counter proposal>" `
       -m $body
   }
   ```

## Rescinding a proposal (0.14.0)

- **Rescind, don't retract in prose (0.14.0).** To cancel a tracked
  request you opened (question / review-request / proposal), use
  `agenttalk rescind --from $SELF --to-request <RID> -m "<why>"`. A prose
  "ignore my last message" moves no thread state — the peer's `wait`
  cannot see it and `check` still reports current. A rescind wakes a
  blocked scoped waiter with exit 3 and flips the thread to
  closed-superseded for every participant. A re-ask after a rescind needs
  a FRESH request_id.

## Before stopping

Before declaring the decision handled or going idle (in a managed wrapper turn skip this: the wrapper
owns the inbox), run:

```powershell
agenttalk sync --for $SELF
agenttalk threads --for $SELF
```

Resolve any `reply-waiting` or `owed-inbound` rows. Do not leave an
accepted/rejected/countered response unread in the inbox.
