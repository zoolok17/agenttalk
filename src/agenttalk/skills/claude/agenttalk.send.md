---
description: Send a single message to the other agent (Codex) over agenttalk. Use for short pings that do not require waiting for a reply.
---

# /agenttalk.send — Send a message to the other agent

You are operating as the **`claude`** agent in this project. The other agent
(typically `codex`) is running in a separate terminal.

## When to use this skill

- You want to tell Codex something but do not need it to reply before you
  continue (e.g. "FYI I'm starting WP02", "I rebased onto main").
- For request/response patterns where you must wait, use
  `/agenttalk.handoff` instead.

## Procedure

1. Parse the user-invocation text for the message body. If absent, ask the
   user concisely for the body.
2. Send the message:
   ```powershell
   agenttalk send --from claude --to codex --kind <kind> --subject "<one-line>" -m "<body>"
   ```
3. `--kind` values: `message` (default), `note`, `question`,
   `review-request`, `review-result`. Use `--meta key=value` for any
   structured payload (e.g. `--meta wp=WP01`).
4. The CLI prints the rendered message — you do not need to repeat it.
5. Do not wait for a reply. Return control to the user, mentioning the
   message id from the CLI output in one short line.

## Constraints

- The other agent must be initialised in the same project. If
  `agenttalk status` shows the recipient is not in the roster, run
  `agenttalk init --here --agents claude,codex --force` and tell the user
  what you did.
- Never send `--kind end` from this skill. Use the `agenttalk end` CLI
  directly only when the user has confirmed the session is over.
