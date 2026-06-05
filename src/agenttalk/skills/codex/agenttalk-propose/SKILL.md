---
name: agenttalk-propose
description: Send a concrete proposal to a named agent over agenttalk and wait for an accept, reject, or counter response. Use when Codex should propose a specific design, plan, scope, or decision for agreement before proceeding.
---

# agenttalk-propose - Propose a concrete solution (codex side)

You are running as a **Codex** agent. Use this skill when you need a
named agent to decide on a specific proposal, not when you merely need
an open-ended answer.

`proposal` is for "I recommend this concrete solution; accept, reject,
or counter it." Use `question` for open questions, `review-request`
for already-implemented work, and `$agenttalk-consult` for
pressure-testing a draft answer to the user.

Proposals are point-to-point. If a whole group needs awareness, send a
follow-up `agenttalk broadcast --kind note`; do not treat a broadcast
as a multi-party acceptance protocol.

## Identity

```bash
SELF="${AGENTTALK_SELF:-codex}"
PEER="${AGENTTALK_PEER:-claude}"
```

`PEER` is the default decision target for the canonical two-agent
pair. In a larger roster, use `agenttalk roster` and choose a specific
agent such as `claude-lead`, `claude-rev`, or the named owner. Always
resolve inside your current shell - env from prior tool calls does
not persist across separate tool-call processes.

If `.agenttalk/` is not under the current directory, pass `--root
<path>` before the subcommand on every invocation, for example
`agenttalk --root <path> propose --from "$SELF" --to "$PEER" ...`. Do
not write `agenttalk propose --root ...`; global options must precede
the subcommand.

## Split-work guard

A proposal must not become a backdoor for splitting implementation
work. Outside a spec-kitty mission, if the proposal assigns ownership
of files/tasks between agents, ask the user first. If the user already
approved the split, state the ownership boundary in the proposal or a
separate `kind=note`, and every implemented piece still needs a
`kind=review-request` cross-review before the work is called done.

This rule applies equally in a team roster: a proposal can lock the
plan, but it cannot silently make a named agent, role, or group own
implementation work.

## Procedure

1. Resolve `SELF` and `PEER`.
2. Choose `TARGET`. Use `$PEER` for the default pair; otherwise pick
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
   ```
4. Send:
   ```bash
   TARGET="${AGENTTALK_PEER:-claude}"
   REQ_ID="pp-$(uuidgen 2>/dev/null || python -c 'import uuid; print(uuid.uuid4())')"
   agenttalk propose --from "$SELF" --to "$TARGET" \
     --subject "<one-line decision>" \
     --meta request_id="$REQ_ID" \
     -m "$BODY"
   ```
   The command writes `kind=proposal`, auto-mints
   `meta.request_id=pp-...` if absent. Passing `REQ_ID` explicitly
   lets the wait below target the proposal thread.
5. Wait for the response:
   ```bash
   agenttalk wait --for "$SELF" --to-request "$REQ_ID" --kind proposal-response --timeout 600
   ```
   The scoped wait ignores unrelated traffic and does not advance your
   global cursor.
6. Match the response by `meta.request_id`.
   - `proposal-response status=accepted`: proceed with the decision.
   - `proposal-response status=rejected`: do not proceed; either revise
     or ask the user.
   - `proposal-response status=countered`: treat the old proposal as
     closed, then read the fresh counter proposal linked by
     `meta.in_reply_to` or `meta.counter_request_id`.

Scoped wait intentionally ignores unrelated traffic. If the proposal
stalls or you suspect another urgent thread is waiting on you, run
`agenttalk sync --for "$SELF"` and `agenttalk threads --for "$SELF"`.
Handle the older or more urgent thread first, then resume the scoped
wait for your proposal. Use `agenttalk reply --to-id <message_id>` or
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
   agenttalk propose --from "$SELF" --to "$TARGET" \
     --in-reply-to <old-request-id> \
     --subject "<counter proposal>" \
     -m "$BODY"
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

Before declaring the decision handled or going idle, run:

```bash
agenttalk sync --for "$SELF"
agenttalk threads --for "$SELF"
```

Resolve any `reply-waiting` or `owed-inbound` rows. Do not leave an
accepted/rejected/countered response unread in the inbox.
