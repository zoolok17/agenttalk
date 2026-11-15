---
description: Show the one-screen agenttalk overview - what needs attention, who is waiting for whom, and how each agent is doing. Read-only; use when asked how the team is doing.
reviewed-against: "0.105"
---

# /agenttalk - One-screen team overview

Run this command and show its output to the user exactly as printed:

```bash
agenttalk overview
```

If the user asks for the paid-model spend as well, add `--enable-budget`. Never add it
otherwise: the spend ledger covers the whole machine.

This skill does nothing else. It does not read or answer messages, does not change anything and
does not start any work. If the command fails, show its error and stop.
