---
description: Send a single message to the peer agent over agenttalk. Use for short pings that do not require waiting for a reply.
---

# /agenttalk.send — Send a message to the peer agent

You are running as a **Claude Code** agent. Your peer (typically a
Codex, but could be another Claude) is running in a separate terminal.

## Identity

Before invoking any `agenttalk` command, resolve your name and the
peer's. Env vars set by the user win; otherwise fall back to the
defaults appropriate for the Claude side:

```powershell
$SELF = if ($env:AGENTTALK_SELF) { $env:AGENTTALK_SELF } else { "claude" }
$PEER = if ($env:AGENTTALK_PEER) { $env:AGENTTALK_PEER } else { "codex" }
```

If the user is running two Claudes (or any same-kind pair), they will
have set distinct `AGENTTALK_SELF` / `AGENTTALK_PEER` per terminal.
Always resolve inside your current shell — env from prior tool calls
does not persist.

## When to use this skill

- You want to tell the peer something but do not need a reply before
  continuing (e.g. "FYI starting WP02", "I rebased onto main").
- For request/response patterns where you must wait, use
  `/agenttalk.handoff` instead.

For true fire-and-forget traffic, use `kind=message` or `kind=note`.
`kind=question` opens a tracked `q-` request thread; use it only when
the peer really owes an answer, and tell the user the question is now
pending. If the answer is needed before you continue, use
`/agenttalk.handoff` instead.

**Do NOT use this skill to coordinate splitting implementation work**
(e.g., "I'll do the frontend, you do the backend") without first
asking the user. See `/agenttalk.handoff` and `/agenttalk.listen` for
the full split-work rules; the short version is: don't split outside
a spec-kitty mission without user approval, and when a split is
approved, every piece MUST be cross-reviewed via `/agenttalk.handoff`.

## Procedure

1. Parse the user-invocation text for the message body. If absent, ask
   the user concisely for the body.
2. Resolve `$SELF` and `$PEER` as above.
3. Send:
   ```powershell
   agenttalk send --from $SELF --to $PEER --kind <kind> --subject "<one-line>" -m "<body>"
   ```
4. `--kind` values: `message` (default), `note`, `question`,
   `review-request`, `review-result`, `proposal`, `proposal-response`.
   Use `note` for informational fire-and-forget and `question` for a
   tracked question. Prefer `/agenttalk.propose` over raw
   `send --kind proposal`. Use `--meta key=value` for any structured
   payload.
5. The CLI prints the rendered message — do not repeat it.
6. Do not wait for a reply. Return control to the user, mentioning the
   message id in one short line. If you sent `kind=question`, also
   mention that it is tracked by `agenttalk threads` until answered.

## Constraints

- The peer must be in the roster. Check with `agenttalk status`. If
  not, run `agenttalk init --here --agents <self>,<peer> --force` and
  tell the user what you did.
- Never send `--kind end` from this skill. Use `agenttalk end --from
  $SELF` directly only when the user has confirmed the session is over.
