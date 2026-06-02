---
name: agenttalk-send
description: Send a single message to the peer agent over agenttalk. Use this when the user asks to "ping the other agent" or "send to <name>" and the request does not require waiting for a reply. Requires `agenttalk init` in the project root.
---

# agenttalk-send — Send a message to the peer agent (codex side)

You are running as a **Codex** agent. Your peer (typically a Claude
Code, but could be another Codex) is in another terminal in the same
project. Messages flow via `agenttalk`, backed by `.agenttalk/` in
the project root.

## Identity

Resolve your name and the peer's in your current shell:

```bash
SELF="${AGENTTALK_SELF:-codex}"
PEER="${AGENTTALK_PEER:-claude}"
```

If the user is running two Codexes (or any same-kind pair), they will
have set distinct `AGENTTALK_SELF` / `AGENTTALK_PEER` per terminal.
Always resolve inside your current shell — env from prior tool calls
does not persist.

## When to trigger

When the user asks Codex to send, ping, message, or notify the peer —
and the request does not require waiting for a reply before
continuing.

For true fire-and-forget traffic, use `kind=message` or `kind=note`.
`kind=question` opens a tracked `q-` request thread; use it only when
the peer really owes an answer, and tell the user the question is now
pending. If the answer is needed before you continue, use
`agenttalk-handoff` instead.

For request/response (e.g. "ask claude to fix X then wait"), use
`agenttalk-handoff`. To act as a passive listener, use
`agenttalk-listen`.

**Do NOT use this skill to coordinate splitting implementation work**
(e.g., "I'll do the frontend, you do the backend") without first
asking the user. See `$agenttalk-handoff` and `$agenttalk-listen` for
the full split-work rules; the short version is: don't split outside
a spec-kitty mission without user approval, and when a split is
approved, every piece MUST be cross-reviewed via `$agenttalk-handoff`.

## Prerequisites

1. Verify `.agenttalk/` exists in the project root. Otherwise tell
   the user `agenttalk init --here --agents <self>,<peer>` is needed.
2. Confirm `agenttalk --help` runs. If missing, install:
   `python -m pip install -e <path-to-agenttalk>`.

## Procedure

```bash
SELF="${AGENTTALK_SELF:-codex}"
PEER="${AGENTTALK_PEER:-claude}"
agenttalk send --from "$SELF" --to "$PEER" --kind <kind> \
  --subject "<one line>" -m "<body>"
```

- `--kind` defaults to `message`. Use `note` for informational
  fire-and-forget, `question` for a tracked question, `review-request`
  / `review-result` for review flows, and `proposal` /
  `proposal-response` only when you are deliberately using the
  proposal protocol. Prefer `$agenttalk-propose` over raw
  `send --kind proposal`.
- `--meta key=value` is repeatable for structured payload.
- The CLI prints the rendered message — do not repeat it.

After sending, return control to the user with a one-line summary
including the message id. If you sent `kind=question`, also mention
that it is tracked by `agenttalk threads` until answered. Do **not**
call `agenttalk wait` from this skill — that is `agenttalk-listen` or
`agenttalk-handoff`.

## Do not

- Do not send `--kind end`. The user should explicitly ask to end the
  session, after which run `agenttalk end --from "$SELF" --reason "..."`.
- Do not modify project files; this skill only sends a message.
