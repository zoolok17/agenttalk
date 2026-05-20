---
description: Send a structured handoff (typically a review request) to the peer agent and block until the reply arrives. Works for both spec-kitty WP reviews and ad-hoc cross-reviews of work you just finished — the receiver mode-detects from meta.
---

# /agenttalk.handoff — Hand work off to the peer and wait for reply

You are running as a **Claude Code** agent. Your peer is in another
terminal. This skill bundles `send` + `wait` into one round-trip so
you can hand work off and get the response in a single tool call.

Works in two modes, distinguished only by what `meta` you include:
- **Spec-kitty mode** — include `mission` / `wp_id`. Receiver routes
  to `/spec-kitty.review` (or equivalent) and applies owned_files
  checks.
- **Ad-hoc cross-review mode** — omit mission/wp_id. Receiver reviews
  the scope declared in your body, optionally verified against
  `base_sha..head_sha`.

## Identity

Resolve your name and the peer's in your current shell:

```powershell
$SELF = if ($env:AGENTTALK_SELF) { $env:AGENTTALK_SELF } else { "claude" }
$PEER = if ($env:AGENTTALK_PEER) { $env:AGENTTALK_PEER } else { "codex" }
```

Always resolve inside your current shell — env from prior tool calls
does not persist across separate tool-call processes.

## Splitting implementation work with the peer

**Outside spec-kitty, do NOT split implementation work with the peer
without first asking the user.** The user invoked you to do a task;
the peer is for review or specific delegated subtasks, not for
unilaterally carving up the work.

If the user explicitly approves a split, then:

1. **State the ownership boundaries up front.** Send a `kind=note`
   that says who owns which files/tasks and who reviews which piece.
2. **Every implemented piece MUST receive a `kind=review-request`
   cross-review** before the overall task is called done. This skill
   is how you do it. This is the whole point of having two agents —
   each catches what the other missed.
3. **Reviews are read-only.** The implementer of each piece fixes
   their own blockers unless the user explicitly changes ownership.

In a spec-kitty mission, ignore this section — spec-kitty's state
machine already assigns implement/review responsibilities per WP.

## When to use this skill

- You finished implementing a WP (spec-kitty) and want review.
- You finished a chunk of organic split work and want the peer to
  cross-review it.
- You have a focused question whose answer determines next steps.
- You need a second opinion before a non-trivial change.

For fire-and-forget (no reply needed), use `/agenttalk.send`.

## Procedure

### 1. Generate a request_id

```powershell
$reqId = [guid]::NewGuid().ToString()
```

Required so the receiver can echo it back in `review-result` and you
can match the verdict to your request.

### 2. Build the meta

Always:
- `request_id=$reqId`

Spec-kitty mode (only if reviewing a spec-kitty WP):
- `mission=<slug>`
- `wp_id=WP##`

Ad-hoc cross-review mode (if available):
- `base_sha=<git rev-parse <merge-base>>`
- `head_sha=<git rev-parse HEAD>`
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

```powershell
agenttalk send --from $SELF --to $PEER --kind review-request `
  --subject "<one-line>" `
  --meta request_id=$reqId `
  --meta base_sha=<sha> --meta head_sha=<sha> `
  -m $body
agenttalk wait --for $SELF --timeout 600
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
  `/agenttalk.handoff` again (new request_id) with the updated work.
- `status=needs-info` → answer the questions via `/agenttalk.send`
  (kind=message), then re-request review.

If the reply is ambiguous or asks for a decision only the human can
make, **stop and ask the user**.

## When you might receive a request while waiting

If your own request is outstanding and an incoming `kind=review-request`
arrives, handle the OLDER `request_id` (or earlier message timestamp)
first to avoid both sides blocking each other. After replying, resume
waiting for your own result.

## Constraints

- Do not loop forever. After 3 consecutive rejected reviews on the
  same scope without convergence, surface to the user.
- If the peer sends `kind=end`, the session is over — do not continue.
