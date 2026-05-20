---
name: agenttalk-handoff
description: Send a structured handoff (typically a review request) to the peer agent and block until the reply arrives. Works for both spec-kitty WP reviews and ad-hoc cross-reviews of work you just finished — the receiver mode-detects from meta.
---

# agenttalk-handoff — Hand work off and wait for reply (codex side)

You are running as a **Codex** agent. Your peer is in another
terminal. This skill bundles `send` + `wait` into one round-trip.

Works in two modes, distinguished only by `meta`:
- **Spec-kitty mode** — include `mission` / `wp_id`. Receiver routes
  to the spec-kitty review workflow.
- **Ad-hoc cross-review mode** — omit mission/wp_id. Receiver reviews
  the scope declared in your body, optionally verified against
  `base_sha..head_sha`.

## Identity

```bash
SELF="${AGENTTALK_SELF:-codex}"
PEER="${AGENTTALK_PEER:-claude}"
```

Always resolve inside your current shell — env from prior tool calls
does not persist across separate tool-call processes.

## Splitting implementation work with the peer

**Outside spec-kitty, do NOT split implementation work with the peer
without first asking the user.** The user invoked you to do a task;
the peer is for review or specific delegated subtasks, not for
unilaterally carving up the work.

If the user explicitly approves a split:

1. **State the ownership boundaries up front.** Send a `kind=note`
   that says who owns which files/tasks and who reviews which piece.
2. **Every implemented piece MUST receive a `kind=review-request`
   cross-review** before the overall task is called done. This skill
   is how you do it. This is the whole point of having two agents.
3. **Reviews are read-only.** The implementer of each piece fixes
   their own blockers unless the user explicitly changes ownership.

In a spec-kitty mission, ignore this section — spec-kitty's state
machine already assigns implement/review responsibilities per WP.

## When to use

- You finished implementing a WP (spec-kitty) and want review.
- You finished a chunk of organic split work and want the peer to
  cross-review it.
- A focused question whose answer determines next steps.
- A second opinion before a non-trivial change.

For fire-and-forget (no reply needed), use `$agenttalk-send`.

## Procedure

### 1. Generate a request_id

```bash
REQ_ID=$(uuidgen 2>/dev/null || python -c 'import uuid; print(uuid.uuid4())')
```

Required for correlation — the receiver echoes it in `review-result`.

### 2. Build the meta

Always:
- `request_id=$REQ_ID`

Spec-kitty mode (only if reviewing a spec-kitty WP):
- `mission=<slug>`
- `wp_id=WP##`

Ad-hoc cross-review mode (if available):
- `base_sha=$(git merge-base ...)`
- `head_sha=$(git rev-parse HEAD)`
- `branch=<name>` (optional)
- `scope=ad-hoc`

### 3. Build the body

For spec-kitty mode: WP id, feature dir, files changed, spec-kitty
command the reviewer should run, non-obvious decisions.

For ad-hoc cross-review, use this template:

```text
## Goal
<one paragraph: what this chunk of work was meant to achieve>

## Files changed
- path/a.py
- path/b.py

## How to verify
<commands to run, fixtures, manual checks>

## Focus areas
<what you want the reviewer to pay extra attention to>

## Known caveats
<things you know are imperfect, deferred decisions, open questions>
```

### 4. Send + wait

```bash
agenttalk send --from "$SELF" --to "$PEER" --kind review-request \
  --subject "<one-line>" \
  --meta request_id="$REQ_ID" \
  --meta base_sha=<sha> --meta head_sha=<sha> \
  -m "$BODY"
agenttalk wait --for "$SELF" --timeout 600
```

Default 10-minute timeout. Extend with `--timeout 1800` for big
reviews, or `0` for no timeout.

If `wait` times out: tell the user and ask whether to keep waiting or
check `agenttalk status` to see whether the peer's `last_seen` is
fresh.

### 5. Act on the reply

Match it by `meta.request_id`:
- `status=approved` → proceed or ship.
- `status=rejected` → apply requested changes, then call
  `$agenttalk-handoff` again (new request_id).
- `status=needs-info` → answer via `$agenttalk-send` (kind=message),
  then re-request review.

If ambiguous or asks for a decision only the human can make,
**stop and ask the user**.

## When you might receive a request while waiting

If your own request is outstanding and an incoming
`kind=review-request` arrives, handle the OLDER `request_id` (or
earlier message timestamp) first to avoid both sides blocking each
other.

## Constraints

- Do not loop forever. After 3 consecutive rejected reviews on the
  same scope without convergence, surface to the user.
- If the peer sends `kind=end`, the session is over.
