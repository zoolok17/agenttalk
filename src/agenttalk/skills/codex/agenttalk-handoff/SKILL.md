---
name: agenttalk-handoff
description: Send a structured handoff, typically a review request, to a named agent and block until the reply arrives. Works for spec-kitty WP reviews and ad-hoc cross-reviews; the receiver mode-detects from meta.
reviewed-against: "0.55"
---

# agenttalk-handoff - Hand work off and wait for reply (codex side)

You are running as a **Codex** agent. This skill bundles `send` and
`wait` into one point-to-point round-trip with a named target agent.
It is for review requests, focused questions, and bounded second
opinions that need a reply before you continue.

Works in two modes, distinguished only by `meta`:
- **Spec-kitty mode** - include `mission` / `wp_id`. Receiver routes
  to the spec-kitty review workflow.
- **Ad-hoc cross-review mode** - omit mission/wp_id. Receiver reviews
  the scope declared in your body, optionally verified against
  `base_sha..head_sha`.

## Identity

```bash
SELF="${AGENTTALK_SELF:-codex}"
PEER="${AGENTTALK_PEER:-claude}"
```

`PEER` is the default target for the canonical two-agent pair. In a
larger roster, use `python -m agenttalk roster` and choose the reviewer or
consultant explicitly, e.g. `claude-rev` or `codex-rev`. Always
resolve inside your current shell - env from prior tool calls does not
persist across separate tool-call processes.

If `.agenttalk/` is not under the current directory, pass `--root
<path>` before the subcommand on every invocation, for example
`python -m agenttalk --root <path> send --from "$SELF" --to "$PEER" ...`. Do
not write `python -m agenttalk send --root ...`; global options must precede the
subcommand.

## Invoking agenttalk under the Codex sandbox

Run bus commands from the current project WORKSPACE cwd, using AGENTTALK_ROOT for that workspace when it is set. If `AGENTTALK_PY` is set, invoke the bus with the pinned interpreter:

```powershell
& "$env:AGENTTALK_PY" -m agenttalk <subcommand> ...
```

If `AGENTTALK_PY` is not set, fall back to the runnable module form:

```bash
python -m agenttalk <subcommand> ...
```

Treat `agenttalk` as the installed/runtime package for this environment. Do NOT cd to, import from, or reference an agenttalk SOURCE checkout outside the workspace for bus I/O: no `..\agenttalk`, no sibling source paths, and no `D:\Projects\claude\agenttalk`. The only source-tree exception is when the current workspace itself is the agenttalk repo being worked on; then `<workspace>\src\agenttalk` is acceptable.

Do NOT run `pip install -e <agenttalk-source>` as an in-turn bus fix. If the runtime import resolves outside the workspace, ask the operator to install agenttalk non-editable into the runtime Python used by `AGENTTALK_PY`, or run the agent from the agenttalk workspace when intentionally developing agenttalk. If `AGENTTALK_PY` exists but cannot execute inside Codex workspace-write, ask the operator to opt in to the Python install directory with Codex `--add-dir` or equivalent config.
## Splitting implementation work with the peer

**Outside spec-kitty, do NOT split implementation work with the peer
without first asking the user.** The user invoked you to do a task;
the peer, target reviewer, or group is for review or specific
delegated subtasks, not for unilaterally carving up the work. Do not
use `kind=proposal`, `$agenttalk-propose`, or broadcast to route
around this rule.

If the user explicitly approves a split:

1. **State the ownership boundaries up front.** Send a `kind=note`
   that says who owns which files/tasks and who reviews which piece.
2. **Every implemented piece MUST receive a `kind=review-request`
   cross-review** before the overall task is called done. This skill
   is how you do it for one named reviewer.
3. **Reviews are read-only.** The implementer of each piece fixes
   their own blockers unless the user explicitly changes ownership.

In a spec-kitty mission, ignore this section - spec-kitty's state
machine already assigns implement/review responsibilities per WP.

## When to use

- You finished implementing a WP (spec-kitty) and want review.
- You finished a chunk of organic split work and want a named peer or
  fresh-review agent to cross-review it.
- A focused question whose answer determines next steps.
- A second opinion before a non-trivial change.

For fire-and-forget (no reply needed), use `$agenttalk-send`. For
parallel input from several agents, use `python -m agenttalk broadcast --kind
question`, then track the broadcast with `python -m agenttalk threads`.

## Procedure

### 1. Choose the target

For the default pair:

```bash
TARGET="${AGENTTALK_PEER:-claude}"
```

For a team, inspect `python -m agenttalk roster` and set `TARGET` to the named
agent the user requested or the appropriate role-suffixed reviewer:

```bash
TARGET="claude-rev"
```

If the roster has more than two agents and no target is obvious, ask
the user which agent should receive the handoff.

### 2. Generate a request_id

```bash
REQ_ID="rq-$(uuidgen 2>/dev/null || python -c 'import uuid; print(uuid.uuid4())')"
```

Required for correlation - the receiver echoes it in `review-result`.
Use the `rq-` prefix for review requests so they stay visually distinct
from proposal ids (`pp-...`).

### 3. Build the meta

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

```bash
python -m agenttalk send --from "$SELF" --to "$TARGET" --kind review-request \
  --subject "<one-line>" \
  --meta request_id="$REQ_ID" \
  --meta base_sha=<sha> --meta head_sha=<sha> \
  -m "$BODY"
python -m agenttalk wait --for "$SELF" --to-request "$REQ_ID" --kind review-result --timeout 600
```

Default 10-minute timeout. Extend with `--timeout 1800` for big
reviews, or `0` for no timeout. The scoped wait ignores unrelated
traffic and does not advance your global cursor.

If `wait` times out: tell the user and ask whether to keep waiting or
check `python -m agenttalk status` to see whether the target's `last_seen` is
fresh. Also run `python -m agenttalk sync --for "$SELF"` and `python -m agenttalk threads
--for "$SELF"`; if your correlated reply is already actionable,
handle it before asking the user.

### 6. Act on the reply

Match it by `meta.request_id`:
- `status=approved` -> proceed or ship only after any required gate check passes.
- `status=rejected` -> apply requested changes, then call
  `$agenttalk-handoff` again (new request_id).
- `status=needs-info` -> answer via `$agenttalk-send` (kind=message),
  then re-request review.

If ambiguous or asks for a decision only the human can make,
**stop and ask the user**.

Before declaring the handoff handled, run:

```bash
python -m agenttalk sync --for "$SELF"
python -m agenttalk threads --for "$SELF"
```

Resolve any `reply-waiting` or `owed-inbound` rows. If multiple
threads are open, use `python -m agenttalk reply --to-id <message_id>` or
`--to-request <request_id>` so the reply echoes the intended
`request_id`.

## When you might receive a request while waiting

Scoped wait intentionally ignores unrelated traffic. If your own
request is outstanding but you suspect another urgent thread may be
waiting on you, run `python -m agenttalk sync --for "$SELF"` and `python -m agenttalk
threads --for "$SELF"`. Handle the older or more urgent thread first
to avoid both sides blocking each other, then resume the scoped wait
for your request.

## Constraints

- Do not loop forever. After 3 consecutive rejected reviews on the
  same scope without convergence, surface to the user.
- If the peer sends `kind=end`, the session is over.

- **Check before irreversible actions (0.14.0).** Immediately before any
  irreversible action tied to a tracked request (merge, release, deploy,
  delete, fire-type actions), run
  `python -m agenttalk check --for $SELF --to-request <RID>`. Exit 3 = the request
  was RESCINDED: hard stop — do not act, and reply on the thread that you
  aborted. Exit 4 = unknown id: treat as stale and re-confirm with the
  counterparty. Only exit 0 (current) clears you to act. When assurance
  gates apply, run the same check with `--gates`; exit 3 also means HOLD.
- **Mark long drafts (0.14.0).** While drafting a long reply on a known
  thread, ping `python -m agenttalk composing --from $SELF --to-request <RID>`
  (repeat roughly every 2 minutes). It extends the peer's scoped wait AND
  shows "(reply in flight)" in their threads/sync, preventing crossing
  messages. Prefer it over a hand-built `--meta request_id=...`.
