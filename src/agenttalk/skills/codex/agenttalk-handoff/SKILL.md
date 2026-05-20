---
name: agenttalk-handoff
description: Send a structured handoff (typically a review request) to the peer agent and block until the reply arrives. Works for both spec-kitty WP reviews and ad-hoc cross-reviews of work you just finished — the receiver mode-detects from meta.
---

# agenttalk-handoff — Hand work off to the peer and wait for reply (codex side)

You are the **`codex`** agent. Your peer is `claude`. This skill bundles
a `send` + `wait` into one round-trip so you can hand work off and get
the response in a single tool call.

Works in two modes, distinguished only by what `meta` you include:
- **Spec-kitty mode** — include `mission` / `wp_id`. Receiver routes to
  `/spec-kitty.review` (or equivalent) and applies owned_files checks.
- **Ad-hoc cross-review mode** — omit mission/wp_id. Receiver reviews
  the scope declared in your body, optionally verified against
  `base_sha..head_sha`.

## When to use

- You finished implementing a WP and want spec-kitty review.
- You finished a chunk of organic split work and want the peer to
  cross-review it.
- You have a focused question whose answer determines what you do next.
- You need a second opinion before a non-trivial change.

For fire-and-forget (no reply needed), use `$agenttalk-send`.

## Procedure

### 1. Generate a request_id

```bash
REQ_ID=$(uuidgen 2>/dev/null || python -c 'import uuid; print(uuid.uuid4())')
```

Use this for correlation. Required so the receiver can echo it back in
`review-result` and you can match the verdict to your request.

### 2. Build the meta

Always:
- `request_id=$REQ_ID`

Spec-kitty mode (only if reviewing a spec-kitty WP):
- `mission=<slug>`
- `wp_id=WP##`

Ad-hoc cross-review mode (if available):
- `base_sha=$(git merge-base ...)`  — what the peer should diff against
- `head_sha=$(git rev-parse HEAD)`  — your current tip
- `branch=<name>` (optional)
- `scope=ad-hoc`

### 3. Build the body

For spec-kitty mode, include:
- WP id and feature dir
- Files you changed
- Spec-kitty command the reviewer should run
- Non-obvious decisions from the diff

For ad-hoc cross-review, use this template:

```text
## Goal
<one paragraph: what this chunk of work was meant to achieve>

## Files changed
- path/a.py
- path/b.py
- ...

## How to verify
<commands to run, fixtures, manual checks>

## Focus areas
<what you want the reviewer to pay extra attention to>

## Known caveats
<things you know are imperfect, deferred decisions, open questions>
```

### 4. Send + wait

```bash
agenttalk send --from codex --to claude --kind review-request \
  --subject "<one-line>" \
  --meta request_id="$REQ_ID" \
  --meta base_sha=<sha> --meta head_sha=<sha> \
  -m "$BODY"
agenttalk wait --for codex --timeout 600
```

Default 10-minute timeout. Extend with `--timeout 1800` for big reviews,
or `0` for no timeout.

If `wait` times out: tell the user and ask whether to keep waiting or
check `agenttalk status` to confirm the peer is still listening.

### 5. Act on the reply

The reply will be a `review-result`. Match it by `meta.request_id`:
- `status=approved` → proceed to next step or ship.
- `status=rejected` → apply requested changes, then call
  `$agenttalk-handoff` again (new request_id) with the updated work.
- `status=needs-info` → answer the questions via `$agenttalk-send`
  (kind=message), then re-request review.

If the reply is ambiguous or asks for a decision only the human can
make, **stop and ask the user**.

## When you might receive a request while waiting

If an incoming `kind=review-request` arrives while you're waiting for
your own reply, you have two choices:
- **Recommended:** break out, handle the incoming request (per
  `$agenttalk-listen`), then resume waiting.
- **Alternative:** ignore until your reply lands.

Handle the older `request_id` first to avoid both sides blocking each
other.

## Constraints

- Do not loop forever. After 3 consecutive rejected reviews on the
  same scope without convergence, surface to the user.
- If the peer sends `kind=end`, the session is over — do not continue.
