---
description: Send a structured handoff, typically a review request, to a named agent and block until the reply arrives. Works for spec-kitty WP reviews and ad-hoc cross-reviews; the receiver mode-detects from meta.
reviewed-against: "0.42"
---

# /agenttalk.handoff - Hand work off to a named agent and wait for reply

You are running as a **Claude Code** agent. This skill bundles `send`
and `wait` into one point-to-point round-trip with a named target agent.
It is for review requests, focused questions, and bounded second
opinions that need a reply before you continue.

Works in two modes, distinguished only by what `meta` you include:
- **Spec-kitty mode** - include `mission` / `wp_id`. Receiver routes
  to `/spec-kitty.review` (or equivalent) and applies owned_files
  checks.
- **Ad-hoc cross-review mode** - omit mission/wp_id. Receiver reviews
  the scope declared in your body, optionally verified against
  `base_sha..head_sha`.

## Identity

Resolve your name and default peer in your current shell:

```powershell
$SELF = if ($env:AGENTTALK_SELF) { $env:AGENTTALK_SELF } else { "claude" }
$PEER = if ($env:AGENTTALK_PEER) { $env:AGENTTALK_PEER } else { "codex" }
```

`PEER` is the default target for the canonical two-agent pair. In a
larger roster, use `agenttalk roster` and choose the reviewer or
consultant explicitly, e.g. `claude-rev` or `codex-rev`. Always
resolve inside your current shell - env from prior tool calls does not
persist across separate tool-call processes.

If `.agenttalk/` is not under the current directory, pass `--root
<path>` before the subcommand on every invocation, for example
`agenttalk --root <path> send --from $SELF --to $PEER ...`. Do not
write `agenttalk send --root ...`; global options must precede the
subcommand.

## Splitting implementation work with the peer

**Outside spec-kitty, do NOT split implementation work with the peer
without first asking the user.** The user invoked you to do a task;
the peer, target reviewer, or group is for review or specific
delegated subtasks, not for unilaterally carving up the work. Do not
use `kind=proposal`, `/agenttalk.propose`, or broadcast to route
around this rule.

If the user explicitly approves a split, then:

1. **State the ownership boundaries up front.** Send a `kind=note`
   that says who owns which files/tasks and who reviews which piece.
2. **Every implemented piece MUST receive a `kind=review-request`
   cross-review** before the overall task is called done. This skill
   is how you do it for one named reviewer.
3. **Reviews are read-only.** The implementer of each piece fixes
   their own blockers unless the user explicitly changes ownership.

In a spec-kitty mission, ignore this section - spec-kitty's state
machine already assigns implement/review responsibilities per WP.

## When to use this skill

- You finished implementing a WP (spec-kitty) and want review.
- You finished a chunk of organic split work and want a named peer or
  fresh-review agent to cross-review it.
- You have a focused question whose answer determines next steps.
- You need a second opinion before a non-trivial change.

For fire-and-forget (no reply needed), use `/agenttalk.send`. For
parallel input from several agents, use `agenttalk broadcast --kind
question`, then track the broadcast with `agenttalk threads`.

## Procedure

### 1. Choose the target

For the default pair:

```powershell
$TARGET = if ($env:AGENTTALK_PEER) { $env:AGENTTALK_PEER } else { "codex" }
```

For a team, inspect `agenttalk roster` and set `$TARGET` to the named
agent the user requested or the appropriate role-suffixed reviewer:

```powershell
$TARGET = "codex-rev"
```

If the roster has more than two agents and no target is obvious, ask
the user which agent should receive the handoff.

### 2. Generate a request_id

```powershell
$reqId = "rq-" + [guid]::NewGuid().ToString()
```

Required so the receiver can echo it back in `review-result` and you
can match the verdict to your request. Use the `rq-` prefix for review
requests so they stay visually distinct from proposal ids (`pp-...`).

### 3. Build the meta

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

### 4. Build the body

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

### 5. Send + wait

```powershell
agenttalk send --from $SELF --to $TARGET --kind review-request `
  --subject "<one-line>" `
  --meta request_id=$reqId `
  --meta base_sha=<sha> --meta head_sha=<sha> `
  -m $body
agenttalk wait --for $SELF --to-request $reqId --kind review-result --timeout 600
```

Default 10-minute timeout. Extend with `--timeout 1800` for big
reviews, or `0` for no timeout. The scoped wait ignores unrelated
traffic and does not advance your global cursor.

If `wait` times out: tell the user and ask whether to keep waiting or
check `agenttalk status` to see whether the target's `last_seen` is
fresh. Also run `agenttalk sync --for $SELF` and `agenttalk threads
--for $SELF`; if your correlated reply is already actionable, handle
it before asking the user.

### 6. Act on the reply

Match it by `meta.request_id`:
- `status=approved` -> proceed or ship only after any required gate check passes.
- `status=rejected` -> apply requested changes, then call
  `/agenttalk.handoff` again (new request_id) with the updated work.
- `status=needs-info` -> answer the questions via `/agenttalk.send`
  (kind=message), then re-request review.

If the reply is ambiguous or asks for a decision only the human can
make, **stop and ask the user**.

Before declaring the handoff handled, run:

```powershell
agenttalk sync --for $SELF
agenttalk threads --for $SELF
```

Resolve any `reply-waiting` or `owed-inbound` rows. If multiple
threads are open, use `agenttalk reply --to-id <message_id>` or
`--to-request <request_id>` so the reply echoes the intended
`request_id`.

## When you might receive a request while waiting

Scoped wait intentionally ignores unrelated traffic. If your own
request is outstanding but you suspect another urgent thread may be
waiting on you, run `agenttalk sync --for $SELF` and `agenttalk
threads --for $SELF`. Handle the older or more urgent thread first to
avoid both sides blocking each other, then resume the scoped wait for
your request.

## Constraints

- Do not loop forever. After 3 consecutive rejected reviews on the
  same scope without convergence, surface to the user.
- If the peer sends `kind=end`, the session is over - do not continue.

- **Check before irreversible actions (0.14.0).** Immediately before any
  irreversible action tied to a tracked request (merge, release, deploy,
  delete, fire-type actions), run
  `agenttalk check --for $SELF --to-request <RID>`. Exit 3 = the request
  was RESCINDED: hard stop — do not act, and reply on the thread that you
  aborted. Exit 4 = unknown id: treat as stale and re-confirm with the
  counterparty. Only exit 0 (current) clears you to act. When assurance
  gates apply, run the same check with `--gates`; exit 3 also means HOLD.
- **Mark long drafts (0.14.0).** While drafting a long reply on a known
  thread, ping `agenttalk composing --from $SELF --to-request <RID>`
  (repeat roughly every 2 minutes). It extends the peer's scoped wait AND
  shows "(reply in flight)" in their threads/sync, preventing crossing
  messages. Prefer it over a hand-built `--meta request_id=...`.
