---
name: agenttalk
description: Show the one-screen agenttalk overview - what needs attention, who is waiting for whom, and how each agent is doing. Read-only; use when asked how the team is doing.
reviewed-against: "0.101"
---

# agenttalk - One-screen team overview

## Invoking agenttalk under the Codex sandbox

Run bus commands from the current project WORKSPACE cwd, using AGENTTALK_ROOT for that workspace when it is set. If `AGENTTALK_PY` is set, invoke the bus with the pinned interpreter. In Bash:

```bash
"$AGENTTALK_PY" -m agenttalk <subcommand> ...
```

In PowerShell:

```powershell
& "$env:AGENTTALK_PY" -m agenttalk <subcommand> ...
```

If `AGENTTALK_PY` is not set, fall back to the runnable module form:

```bash
python -m agenttalk <subcommand> ...
```

Treat `agenttalk` as the installed/runtime package for this environment. Do NOT cd to, import from, or reference an agenttalk SOURCE checkout outside the workspace for bus I/O: no `..\agenttalk`, and no sibling source paths. The only source-tree exception is when the current workspace itself is the agenttalk repo being worked on; then `<workspace>\src\agenttalk` is acceptable.

Do NOT run `pip install -e <agenttalk-source>` as an in-turn bus fix. If the runtime import resolves outside the workspace, ask the operator to install agenttalk non-editable into the runtime Python used by `AGENTTALK_PY`, or run the agent from the agenttalk workspace when intentionally developing agenttalk. If `AGENTTALK_PY` exists but cannot execute inside Codex workspace-write, ask the operator to opt in to the Python install directory with Codex `--add-dir` or equivalent config.

## What to run

Run the `overview` subcommand in the form above and show its output to the user exactly as printed:

```powershell
& "$env:AGENTTALK_PY" -m agenttalk overview
```

```bash
python -m agenttalk overview
```

If the user asks for the paid-model spend as well, add `--enable-budget`. Never add it otherwise: the spend ledger covers the whole machine.

This skill does nothing else. It does not read or answer messages, does not change anything and does not start any work. If the command fails, show its error and stop.
