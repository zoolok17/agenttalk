---
name: agenttalk-send
description: Send a single message to a named agent, or broadcast a note/question to a group, over agenttalk. Use when the request does not require waiting for a reply. Requires `agenttalk init` in the project root.
---

# agenttalk-send - Send a message to a named agent or group (codex side)

You are running as a **Codex** agent. Other agents are in separate
terminals in the same project. Messages flow via `agenttalk`, backed
by `.agenttalk/` in the project root.

## Identity

Resolve your name and the default peer in your current shell:

```bash
SELF="${AGENTTALK_SELF:-codex}"
PEER="${AGENTTALK_PEER:-claude}"
```

`PEER` is only the compatibility default for the canonical two-agent
pair. In a roster with more than two agents, inspect `agenttalk roster`
and use an explicit `--to <agent>`, `--to-group <group>`, or `--all`.
If the user is running two Codexes (or any same-kind team), they will
have set distinct `AGENTTALK_SELF` per terminal and may set
`AGENTTALK_PEER` only for the default point-to-point partner. Always
resolve inside your current shell - env from prior tool calls does
not persist.

If `.agenttalk/` is not under the current directory, pass `--root
<path>` before the subcommand on every invocation, for example
`agenttalk --root <path> send --from "$SELF" --to "$PEER" ...`. Do
not write `agenttalk send --root ...`; global options must precede the
subcommand.

## When to trigger

When the user asks Codex to send, ping, message, notify a named agent,
or broadcast to a group - and the request does not require waiting for
a reply before continuing.

For true fire-and-forget traffic, use `kind=message` or `kind=note`.
`kind=question` opens a tracked `q-` request thread for point-to-point
sends. `agenttalk broadcast --kind question` opens a tracked
broadcast thread with one obligation per recipient. Use either only
when the recipient(s) really owe an answer, and tell the user the
question is now pending. If the answer is needed before you continue,
use `agenttalk-handoff` instead.

For request/response (e.g. "ask claude-rev to review X then wait"),
use `agenttalk-handoff`. To act as a passive listener, use
`agenttalk-listen`.

**Do NOT use this skill to coordinate splitting implementation work**
(e.g., "I'll do the frontend, you do the backend") without first
asking the user. See `$agenttalk-handoff` and `$agenttalk-listen` for
the full split-work rules; the short version is: don't split outside
a spec-kitty mission without user approval, and when a split is
approved, every piece MUST be cross-reviewed via `$agenttalk-handoff`.

## Prerequisites

1. Verify `.agenttalk/` exists in the project root. Otherwise tell
   the user `agenttalk init --here --agents <agent-list>` is needed.
2. Confirm `agenttalk --help` runs. If missing, install:
   `python -m pip install -e <path-to-agenttalk>`.
3. For team sends, run `agenttalk roster` if the target or group is
   not obvious.

## Procedure

Point-to-point:

```bash
SELF="${AGENTTALK_SELF:-codex}"
PEER="${AGENTTALK_PEER:-claude}"
TARGET="<agent-name-or-$PEER>"
BODY="<body>"
printf '%s\n' "$BODY" | agenttalk send --from "$SELF" --to "$TARGET" --kind <kind> \
  --subject "<one line>" --file -
```

Broadcast:

```bash
SELF="${AGENTTALK_SELF:-codex}"
BODY="<body>"
printf '%s\n' "$BODY" | agenttalk broadcast --from "$SELF" --to-group <group> --kind <message|note|question> \
  --subject "<one line>" --file -
# or:
printf '%s\n' "$BODY" | agenttalk broadcast --from "$SELF" --all --kind <message|note|question> \
  --subject "<one line>" --file -
```

- `--kind` defaults to `message`. Use `note` for informational
  fire-and-forget, `question` for a tracked question, `review-request`
  / `review-result` for review flows, and `proposal` /
  `proposal-response` only when you are deliberately using the
  proposal protocol. Prefer `$agenttalk-propose` over raw
  `send --kind proposal`.
- `--meta key=value` is repeatable for structured payload.
- Avoid inline `-m "<body>"` for multi-line text, apostrophes,
  backslashes, or Windows paths. Put the text in a shell variable and
  pipe it to `--file -`, use a PowerShell here-string on Windows, or
  use `--file <path>` for saved text. Put machine-readable paths and
  roots in `--meta key=value`, not only in prose.
- Broadcast supports `message`, `note`, and `question`. It fans out
  one message per recipient, excluding the sender, and prints a shared
  `broadcast_id` / `request_id`.
- The CLI prints the rendered message - do not repeat it.

After sending, return control to the user with a one-line summary
including the message id or broadcast id. If you sent `kind=question`,
also mention that it is tracked by `agenttalk threads` until answered.
Do **not** call `agenttalk wait` from this skill - that is
`agenttalk-listen` or `agenttalk-handoff`.

## Do not

- Do not send `--kind end`. The user should explicitly ask to end the
  session, after which run `agenttalk end --from "$SELF" --reason "..."`.
- Do not modify project files; this skill only sends a message.
- Do not guess a target in a multi-agent roster. Ask the user or use a
  named group they already requested.

- **Rescind, don't retract in prose (0.14.0).** To cancel a tracked
  request you opened (question / review-request / proposal), use
  `agenttalk rescind --from $SELF --to-request <RID> -m "<why>"`. A prose
  "ignore my last message" moves no thread state — the peer's `wait`
  cannot see it and `check` still reports current. A rescind wakes a
  blocked scoped waiter with exit 3 and flips the thread to
  closed-superseded for every participant. A re-ask after a rescind needs
  a FRESH request_id.

- **Not-applicable beats placeholder acks (0.15.0).** A broadcast
  question that does not concern your role gets
  `agenttalk reply --to-request <bid> --na` — it closes your obligation
  and shows the asker "(n/a)" instead of a fake answer. Never
  placeholder-ack, never go silent. (Refused on review-request/proposal
  threads — those need their typed responses.)
