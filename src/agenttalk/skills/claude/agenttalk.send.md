---
description: Send a single message to a named agent, or broadcast a note/question to a group, over agenttalk. Use for short pings that do not require waiting for a reply.
reviewed-against: "0.42"
---

# /agenttalk.send - Send a message to a named agent or group

You are running as a **Claude Code** agent. Other agents are in
separate terminals in the same project. Messages flow via `agenttalk`,
backed by `.agenttalk/` in the project root.

## Identity

Before invoking any `agenttalk` command, resolve your name and the
default peer's. Env vars set by the user win; otherwise fall back to
the defaults appropriate for the Claude side:

```powershell
$SELF = if ($env:AGENTTALK_SELF) { $env:AGENTTALK_SELF } else { "claude" }
$PEER = if ($env:AGENTTALK_PEER) { $env:AGENTTALK_PEER } else { "codex" }
```

`PEER` is only the compatibility default for the canonical two-agent
pair. In a roster with more than two agents, inspect `agenttalk roster`
and use an explicit `--to <agent>`, `--to-group <group>`, or `--all`.
If the user is running two Claudes (or any same-kind team), they will
have set distinct `AGENTTALK_SELF` per terminal and may set
`AGENTTALK_PEER` only for the default point-to-point partner. Always
resolve inside your current shell - env from prior tool calls does
not persist.

If `.agenttalk/` is not under the current directory, pass `--root
<path>` before the subcommand on every invocation, for example
`agenttalk --root <path> send --from $SELF --to $PEER ...`. Do not
write `agenttalk send --root ...`; global options must precede the
subcommand.

## When to use this skill

- You want to tell a named agent something but do not need a reply
  before continuing (e.g. "FYI starting WP02", "I rebased onto main").
- You want to broadcast a note or question to a named group or to
  `--all`.
- For request/response patterns where you must wait, use
  `/agenttalk.handoff` instead.

For true fire-and-forget traffic, use `kind=message` or `kind=note`.
`kind=question` opens a tracked `q-` request thread for point-to-point
sends. `agenttalk broadcast --kind question` opens a tracked
broadcast thread with one obligation per recipient. Use either only
when the recipient(s) really owe an answer, and tell the user the
question is now pending. If the answer is needed before you continue,
use `/agenttalk.handoff` instead.

**Do NOT use this skill to coordinate splitting implementation work**
(e.g., "I'll do the frontend, you do the backend") without first
asking the user. See `/agenttalk.handoff` and `/agenttalk.listen` for
the full split-work rules; the short version is: don't split outside
a spec-kitty mission without user approval, and when a split is
approved, every piece MUST be cross-reviewed via `/agenttalk.handoff`.

## Procedure

1. Parse the user-invocation text for the message body and target. If
   the body is absent, ask the user concisely for it. If the roster
   has more than two agents and the target/group is absent, ask for
   the target instead of guessing.
2. Resolve `$SELF` and `$PEER` as above.
3. For point-to-point, send:
   ```powershell
   $TARGET = "<agent-name-or-$PEER>"
   @'
<body>
'@ | agenttalk send --from $SELF --to $TARGET --kind <kind> --subject "<one-line>" --file -
   ```
4. For broadcast, send:
   ```powershell
   @'
<body>
'@ | agenttalk broadcast --from $SELF --to-group <group> --kind <message|note|question> `
     --subject "<one-line>" --file -
   # or:
   @'
<body>
'@ | agenttalk broadcast --from $SELF --all --kind <message|note|question> `
     --subject "<one-line>" --file -
   ```
5. `--kind` values for point-to-point sends: `message` (default),
   `note`, `question`, `review-request`, `review-result`, `proposal`,
   `proposal-response`. Use `note` for informational fire-and-forget
   and `question` for a tracked question. Prefer
   `/agenttalk.propose` over raw `send --kind proposal`. Use
   `--meta key=value` for structured payload.
   Avoid inline `-m "<body>"` for multi-line text, apostrophes,
   backslashes, or Windows paths. Pipe a here-string to `--file -`, or
   use `--file <path>` for saved text. Put machine-readable paths and
   roots in `--meta key=value`, not only in prose.
6. Broadcast supports `message`, `note`, and `question`. It fans out
   one message per recipient, excluding the sender, and prints a
   shared `broadcast_id` / `request_id`.
7. The CLI prints the rendered message - do not repeat it.
8. Do not wait for a reply. Return control to the user, mentioning the
   message id or broadcast id in one short line. If you sent
   `kind=question`, also mention that it is tracked by
   `agenttalk threads` until answered.

## Constraints

- The target agent or group must be in the roster. Check with
  `agenttalk roster`. Add agents or groups with explicit roster admin
  commands only when the user asks for that setup.
- Never send `--kind end` from this skill. Use `agenttalk end --from
  $SELF` directly only when the user has confirmed the session is over.

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
