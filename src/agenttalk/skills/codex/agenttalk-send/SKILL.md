---
name: agenttalk-send
description: Send a single message to the other agent (typically Claude Code) over the project's local agenttalk message bus. Use this when the user asks Codex to "ping Claude", "tell the other agent", "send to claude", or otherwise wants a one-shot note that does not require waiting for a reply. Requires `agenttalk init` to have been run in the project (look for a `.agenttalk/` dir).
---

# agenttalk-send — Send a message to the other agent

You are operating as the **`codex`** agent. The other agent (typically
`claude`) is running in another terminal in the same project. Messages
are exchanged via the `agenttalk` CLI, backed by `.agenttalk/` in the
project root.

## When to trigger

Trigger this skill when the user asks Codex to send, ping, message, or
notify the other agent — and the request does not require waiting for a
reply before continuing.

If the user wants a request/response (e.g. "ask claude to fix X then
wait"), use the `agenttalk-handoff` skill instead. If the user wants
Codex to act as the passive reviewer for a Claude-led implementation,
use `agenttalk-listen`.

## Prerequisites

1. Verify there is a `.agenttalk/` directory in the project root.
   Otherwise tell the user `agenttalk init --here --agents claude,codex`
   is needed first.
2. Confirm `agenttalk --help` runs. If the command is missing, install
   it: `python -m pip install -e <path-to-agenttalk>` (the user knows
   the path; ask if unsure).

## Procedure

Send the message:

```bash
agenttalk send --from codex --to claude --kind <kind> --subject "<one line>" -m "<body>"
```

- `--kind` defaults to `message`. Use `note`, `question`, or
  `review-result` (with `--meta status=approved|rejected`) when
  appropriate.
- Pass structured payload via `--meta key=value` (repeatable).
- The CLI prints the rendered message. You do not need to repeat it.

After sending, return control to the user with a one-line summary
including the message id from the CLI output. Do **not** call
`agenttalk wait` from this skill — that is the job of `agenttalk-listen`
or `agenttalk-handoff`.

## Do not

- Do not send `--kind end` from this skill. The user should explicitly
  ask to end the session, after which run
  `agenttalk end --from codex --reason "..."`.
- Do not modify project files; this skill only sends a message.
