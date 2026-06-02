---
name: agenttalk-propose
description: Send a concrete proposal to the peer agent over agenttalk and wait for an accept, reject, or counter response. Use when Codex should propose a specific design, plan, scope, or decision for peer agreement before proceeding.
---

# agenttalk-propose - Propose a concrete solution (codex side)

You are running as a **Codex** agent. Your peer is in another
terminal. Use this skill when you need the peer to decide on a
specific proposal, not when you merely need an open-ended answer.

`proposal` is for "I recommend this concrete solution; accept, reject,
or counter it." Use `question` for open questions, `review-request`
for already-implemented work, and `$agenttalk-consult` for
pressure-testing a draft answer to the user.

## Identity

```bash
SELF="${AGENTTALK_SELF:-codex}"
PEER="${AGENTTALK_PEER:-claude}"
```

Always resolve inside your current shell - env from prior tool calls
does not persist across separate tool-call processes.

## Split-work guard

A proposal must not become a backdoor for splitting implementation
work. Outside a spec-kitty mission, if the proposal assigns ownership
of files/tasks between agents, ask the user first. If the user already
approved the split, state the ownership boundary in the proposal or a
separate `kind=note`, and every implemented piece still needs a
`kind=review-request` cross-review before the work is called done.

## Procedure

1. Resolve `SELF` and `PEER`.
2. Build the proposal body:
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
   ```
3. Send:
   ```bash
   agenttalk propose --from "$SELF" --to "$PEER" \
     --subject "<one-line decision>" \
     -m "$BODY"
   ```
   The command writes `kind=proposal`, auto-mints
   `meta.request_id=pp-...` if absent, and prints the proposal id
   unless `--quiet`.
4. Wait for the response:
   ```bash
   agenttalk wait --for "$SELF" --timeout 600
   ```
5. Match the response by `meta.request_id`.
   - `proposal-response status=accepted`: proceed with the decision.
   - `proposal-response status=rejected`: do not proceed; either revise
     or ask the user.
   - `proposal-response status=countered`: treat the old proposal as
     closed, then read the fresh counter proposal linked by
     `meta.in_reply_to` or `meta.counter_request_id`.

If an unrelated message arrives while waiting, handle the older or more
urgent thread first. Use `agenttalk threads --for "$SELF"` to see open
obligations, and use `agenttalk reply --to-id <message_id>` or
`--to-request <request_id>` when replying inside a specific thread.

## Countering a proposal

To counter a proposal you received:

1. Close the old proposal:
   ```bash
   agenttalk reply --from "$SELF" --kind proposal-response \
     --meta status=countered \
     -m "<why a counter is needed>"
   ```
2. Send the counter as a fresh proposal:
   ```bash
   agenttalk propose --from "$SELF" --to "$PEER" \
     --in-reply-to <old-request-id> \
     --subject "<counter proposal>" \
     -m "$BODY"
   ```

## Before stopping

Before declaring the decision handled or going idle, run:

```bash
agenttalk threads --for "$SELF"
```

Resolve any `reply-waiting` or `owed-inbound` rows. Do not leave an
accepted/rejected/countered response unread in the inbox.
