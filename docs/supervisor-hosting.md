# Supervisor Hosting

Audience: operators running an agenttalk team on Windows.

`agenttalk supervise --init` writes these files under `.agenttalk/`:

- `supervisor.json`: supervisor configuration.
- `supervisor.ps1`: the foreground supervisor loop.
- `supervisor-task.ps1`: a Windows Scheduled Task install/status helper.
- `deadman.ps1`: a content-blind mail-age SLO check wrapper.
- `bin/agenttalk.cmd`: a project-local shim that runs the pinned Python.

## PowerShell host

The generated Windows supervisor requires **PowerShell Core 7+**. Stable 7.4+
is recommended and quiet. Stable 7.0-7.3 runs with an end-of-life warning;
every prerelease warns; Windows PowerShell 5.1 and Core 6 are refused.

Select the host once per project. Automatic selection probes only canonical
PowerShell 7 locations under native Program Files roots:

```powershell
$pwshPath = (agenttalk supervise --select-pwsh | ConvertFrom-Json).path
```

To use a portable or nonstandard installation, provide its absolute executable
path. An explicit candidate is terminal: if it fails validation, the command
fails instead of falling through to Program Files.

```powershell
$pwshPath = (agenttalk supervise --select-pwsh --pwsh 'D:\Tools\PowerShell\pwsh.exe' |
  ConvertFrom-Json).path
```

The selection records the canonical path, Core version, probe time, native file
identity, revision, and fingerprint in `.agenttalk/powershell-host.json`. PATH
entries and registered task actions are diagnostics/data only; agenttalk never
auto-executes either to establish trust. This is an anti-accident consistency
control under the same-user model, not signer, ACL, mapped-image, or DLL-tree
attestation.

For a direct foreground host, launch the generated script through the selected
absolute path:

```powershell
& $pwshPath -NoLogo -NoProfile -NonInteractive `
  -File .\.agenttalk\supervisor.ps1
```

## Scheduled Task

The supported durable host is a current-user Scheduled Task. It starts at logon,
runs from the project root, and freezes the selected absolute Core host in its
single action. Run the helper under that same host:

```powershell
& $pwshPath -NoLogo -NoProfile -NonInteractive `
  -File .\.agenttalk\supervisor-task.ps1 -Action install
& $pwshPath -NoLogo -NoProfile -NonInteractive `
  -File .\.agenttalk\supervisor-task.ps1 -Action status
```

Task `start` compares the registered action to the selected path and this
checkout without executing the action path. `status`, `stop`, and `uninstall`
remain usable for recovery when a task binding or generated artifact set is
stale. A custom `-TaskName` is persisted in the selection record.

To change the task host, use the deliberate migration sequence. Replace the
example path and task name as needed:

```powershell
& $pwshPath -NoLogo -NoProfile -NonInteractive -File .\.agenttalk\supervisor-task.ps1 -Action stop -TaskName 'agenttalk-supervisor'
# Wait until status is not Running and the old supervisor process has exited.
& $pwshPath -NoLogo -NoProfile -NonInteractive -File .\.agenttalk\supervisor-task.ps1 -Action uninstall -TaskName 'agenttalk-supervisor'
$pwshPath = (agenttalk supervise --select-pwsh --pwsh 'D:\Tools\PowerShell\pwsh.exe' | ConvertFrom-Json).path
agenttalk supervise --refresh-scripts
& $pwshPath -NoLogo -NoProfile -NonInteractive -File .\.agenttalk\supervisor-task.ps1 -Action install -TaskName 'agenttalk-supervisor'
& $pwshPath -NoLogo -NoProfile -NonInteractive -File .\.agenttalk\supervisor-task.ps1 -Action start -TaskName 'agenttalk-supervisor'
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
& $pwshPath -NoLogo -NoProfile -NonInteractive `
  -File .\.agenttalk\deadman.ps1 -ThresholdSeconds 900 -Json
```

## State Recovery and Freshness

The monitor persists launch/session/backoff bookkeeping in
`.agenttalk/supervisor-state.json` and preserves one validated previous
generation in `.bak`. A valid backup may be used read-only when the primary is
corrupt; that read does not rewrite the primary. If both copies are invalid,
the supervisor fails closed and emits no plan/action until an operator repairs
the state deliberately.

Cooperating state writers use cross-process locks, but those locks are not a
security boundary against another same-user process. Heartbeat timestamps are
future-bounded: a value beyond the configured skew allowance cannot authorize
liveness. Wrapper waiting markers carry unique tokens, so old teardown removes
only the marker generation it created.

Every JSON/TOML artifact the generated PowerShell writes (state, snapshots, and the
per-agent codex `config.toml` seed) is written **BOM-free** (UTF-8 without a byte-order
mark), and readers of legacy/operator/PowerShell-written inputs remain BOM-tolerant.
The three generated `.ps1` files and `bin/agenttalk.cmd` are also BOM-free now that
Windows PowerShell 5.1 is unsupported. This supersedes only D-26's former generated
script exception; its incident evidence and tolerant-reader defense remain in force.

All four generated artifacts carry the same deterministic schema/generation marker
and are checked against their exact current rendered content. Refresh stages and
replaces each file separately, so it is not group-atomic. A partial set is detected
loudly and rerunning this command converges without rewriting `supervisor.json` or
runtime state:

```powershell
agenttalk supervise --refresh-scripts
```

Refresh excludes a claimed supervisor through the lifecycle lock. It does not prove
that a same-selected-Core process has not already parsed old bytes before claiming;
that narrow launcher-mutex race remains a documented limitation.

## Windows Watchdog Residuals

The wrapped-turn watchdog no longer starts `taskkill.exe`. It calls
`os.kill(pid, signal.SIGTERM)` for a start-time-verified Windows target; Windows
implements that as abrupt process termination. This eliminates the
popup-producing `taskkill.exe` subprocess path. The production reporter's
desktop-heap exhaustion diagnosis is plausible but is not an upstream-confirmed
root cause. Windows snapshot and start-time helpers launch CIM through the current
validated selected Core host; selection, TTL, or native-identity ambiguity returns no
snapshot and therefore no kill. PID reuse remains possible after the separate recheck, and the
leaf-first snapshot operation is not an atomic tree kill. These are follow-up
hardening items, not blockers for this narrow fix. Size service recovery
thresholds with those residuals in mind.

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
deployment. Prefer a wrapper such as WinSW or NSSM and point it at the selected
absolute `pwsh.exe` plus the generated script, with explicit paths and working
directory.

Service hosting runs in session 0 and may not have the same user profile,
credentials, PATH, desktop interaction, or token behavior as the interactive
agent CLIs. Validate Python, CLI auth, Codex/Claude home directories, network
access, and sandbox permissions under the exact service account before relying
on it.
