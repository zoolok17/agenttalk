# Supervisor Hosting

Audience: operators running an agenttalk team on Windows.

`agenttalk supervise --init` writes these files under `.agenttalk/`:

- `supervisor.json`: supervisor configuration.
- `supervisor.ps1`: the foreground supervisor loop.
- `supervisor-task.ps1`: a Windows Scheduled Task install/status helper.
- `deadman.ps1`: a content-blind mail-age SLO check wrapper.
- `bin/agenttalk.cmd`: a project-local shim that runs the pinned Python.

## Scheduled Task

The supported durable host is a current-user Scheduled Task. It starts at logon,
runs from the project root, and launches:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File <root>\.agenttalk\supervisor.ps1 -Quiet
```

Install and inspect it from the checkout:

```powershell
.\.agenttalk\supervisor-task.ps1 -Action install
.\.agenttalk\supervisor-task.ps1 -Action status
```

The generated task helper sets `StartWhenAvailable`, `MultipleInstances
IgnoreNew`, restart-on-failure, and no execution time limit. Status prints
`LastRunTime`, `LastTaskResult`, the command path, arguments, working directory,
and `supervisor.ps1` path.

## Kill Switch

Create `.agenttalk\supervisor.kill` to disable mutating supervisor automation.
The file contents are ignored; empty, non-empty, or corrupt all mean disabled.

While the kill switch exists, read-only commands such as `supervise --report`,
`supervise --plan`, `deadman`, `status`, `threads`, `sync`, and `wait` remain
usable. The supervisor refuses kills, relaunches, seeding, launch-request
claim/archive, marker clearing, bus notify, and supervisor-state reconciliation
writes. Remove the file to re-enable automation.

## Deadman

`agenttalk deadman --threshold-seconds 900 --json` checks actionable mail age
without emitting or semantically inspecting message bodies or subjects. It
reuses the normal queue scan, but its report is content-blind and it does not
read `supervisor-state.json`. It alarms on stale owed inbound work and stale
control messages such as `wake`. Stale unread responses are reported separately
and become alarming only when configured or when `--alarm-unread-response` is
passed.

The generated wrapper is:

```powershell
.\.agenttalk\deadman.ps1 -ThresholdSeconds 900 -Json
```

## Degraded Mode

The supervisor is a convenience layer, not the message broker. If it is stopped,
crashed, disabled, or killed, every protocol step remains hand-operable through
the bus:

```powershell
agenttalk threads --for <agent>
agenttalk status
agenttalk sync --for <agent>
agenttalk wait --for <agent> --timeout 1800
agenttalk reply --to-request <rid> -m "<message>"
agenttalk send --from <agent> --to <peer> --kind message -m "<message>"
agenttalk request-restart --for <agent> --from <lead> --reason "<reason>"
```

Operators can also launch an agent window manually with the same identity
environment (`AGENTTALK_ROOT`, `AGENTTALK_SELF`, and the usual listen or wrap
command) when the host layer is unavailable. Keep working manually, then remove
`supervisor.kill` or restart the Scheduled Task when automation should resume.

## Windows Service Caveats

A Windows Service can be useful for a headless host, but it is an advanced
deployment. Prefer a wrapper such as WinSW or NSSM and point it at the generated
PowerShell command with explicit absolute paths and working directory.

Service hosting runs in session 0 and may not have the same user profile,
credentials, PATH, desktop interaction, or token behavior as the interactive
agent CLIs. Validate Python, CLI auth, Codex/Claude home directories, network
access, and sandbox permissions under the exact service account before relying
on it.
