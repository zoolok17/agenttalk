# Team Setup — Native Work & Evidence Spine (second laptop)

**Purpose.** Reproduce, from a bare clone, the supervised Claude+Codex team that
delivers `docs/WORK-PACKAGE-native-work-spine.md` (D1→D4). Every command here was
executed on this machine; every deviation from the shipped docs is called out
with the reason.

**Scope.** This documents the *team and supervisor*, not the feature. The feature
design lives in `docs/RFC-native-work-spine.md`.

---

## 0. Machine facts (fill these in for a different host)

| Thing | Path / value on this host | How to re-derive |
|---|---|---|
| Repo | `C:\Projects\agenttalk` | — |
| Branch | `feature/native-work-spine` | `git checkout -b feature/native-work-spine origin/master` |
| Python | `C:\Users\milos\AppData\Local\Programs\Python\Python314\python.exe` (3.14.6) | `py -0p` |
| Claude CLI | `C:\Users\milos\.local\bin\claude.exe` (2.1.214) | `(Get-Command claude).Source` |
| Codex CLI | `C:\Users\milos\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe` (0.144.5) | see §2 |
| PowerShell Core | `C:\Tools\PowerShell\7\pwsh.exe` (7.6.3 Core) | see §3 |
| Bus root | `C:\Projects\agenttalk\.agenttalk` (gitignored) | `agenttalk whoami` |

---

## 1. The canonical invocation (read this first)

This repo is a **source checkout that is newer than the installed package**
(source `0.78.1` vs. the global `agenttalk.exe` `0.78.0`). A bare `agenttalk ...`
therefore runs the **wrong code**, and a naive `pytest` tests the wrong code.

Pin every invocation to the source tree:

```powershell
$env:PYTHONPATH = "C:\Projects\agenttalk\src"
$py = "C:\Users\milos\AppData\Local\Programs\Python\Python314\python.exe"
& $py -m agenttalk --version      # must print 0.78.1, not 0.78.0
```

Everything below assumes `$py` and `PYTHONPATH` are set as above. This is
work-package §8.6 ("gate discipline gotcha") — it is not optional.

> The supervisor handles this for the agents automatically: its launch executor
> applies `AGENTTALK_ROOT`, `AGENTTALK_PY`, and `PYTHONPATH=<repo>/src` (detected
> source checkout) before `Start-Process`, then the per-agent `env` block. That
> is the work-package §8.5 env recipe, satisfied by construction — you do **not**
> hand-set it per agent.

---

## 2. Prerequisites

### Real executables, never shims

The supervisor spawns with `shell=False`, so a `.cmd`/`.bat`/`.ps1` shim is a
hard launch failure (`wrapper/run.py:625`) — a shim hands off and exits, and the
supervisor would track a dead pid.

**Codex on this host is fine, and the stable path is the one to pin.**
`...\Programs\OpenAI\Codex\bin` is a symlink to
`~\.codex\packages\standalone\current\bin`, and `current` symlinks to the
versioned release. The file is a real 341 MB native PE. The wrapper resolves
symlinks (`run.py:536`), so pinning the **stable** path auto-follows Codex
upgrades — prefer it over the versioned path.

```powershell
Get-Item "C:\Users\milos\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe" |
  Select-Object FullName, Length, @{n='Target';e={$_.ResolvedTarget}}
```

### Skills

`agenttalk install-skills` installs to `~` (`~/.claude/commands`,
`~/.claude/skills`, `~/.codex/skills`) — **one install covers the whole fleet on
this laptop**, there is no per-agent or per-project step. Already in sync here
(`doctor` reports 7/7 + 7/7 + 52/52).

---

## 3. PowerShell Core — the portable-install deviation

The supervisor requires PowerShell **Core 7+**, and agenttalk **refuses the
Microsoft Store / WindowsApps build**:

```
PowerShell Core host selection failed: WindowsApps aliases are unsupported;
select the real local pwsh.exe
```

This host had *only* the Store build (winget-installed as MSIX) and no admin
rights, so auto-discovery — which considers only canonical `C:\Program Files`
installations — failed with `winerror 3`.

**Resolution used: the official portable zip, no admin, fully reversible.**

```powershell
# 1. Resolve + download the official asset (verify size against the API)
$rel = Invoke-RestMethod "https://api.github.com/repos/PowerShell/PowerShell/releases/tags/v7.6.3" `
       -Headers @{ 'User-Agent'='agenttalk-setup' }
($rel.assets | Where-Object name -eq 'PowerShell-7.6.3-win-x64.zip') | Select-Object name,size,browser_download_url

Invoke-WebRequest $url -OutFile $zip -UseBasicParsing

# 2. Extract to a user-writable location
Expand-Archive $zip -DestinationPath "C:\Tools\PowerShell\7" -Force

# 3. VERIFY before trusting it
& "C:\Tools\PowerShell\7\pwsh.exe" -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion; $PSVersionTable.PSEdition'
(Get-AuthenticodeSignature "C:\Tools\PowerShell\7\pwsh.exe").Status   # must be Valid, signer = Microsoft Corporation

# 4. Record it with agenttalk (explicit path is TERMINAL — it never falls back)
& $py -m agenttalk supervise --select-pwsh --pwsh "C:\Tools\PowerShell\7\pwsh.exe"
```

Confirm with `& $py -m agenttalk doctor` → `[ok] powershell_host`.

> **Gotcha (undocumented).** A bare `supervise --select-pwsh` **re-probes** and
> will fail again on this host — it does not return the recorded selection. To
> get the recorded host back, read it:
> ```powershell
> $pwsh = (Get-Content .\.agenttalk\powershell-host.json -Raw | ConvertFrom-Json).path
> ```
> Or pass `--pwsh <abs>` every time.

**To back out:** delete `C:\Tools\PowerShell\7`. Nothing else on the system was
touched — no MSI, no PATH change, no registry.

---

## 4. Bus + roster

```powershell
git checkout -b feature/native-work-spine origin/master

& $py -m agenttalk init --here --agents claude-lead,claude-dev,codex-dev,claude-rev,codex-rev

& $py -m agenttalk roster set-role claude-lead lead
& $py -m agenttalk roster set-role claude-dev  dev
& $py -m agenttalk roster set-role codex-dev   dev
& $py -m agenttalk roster set-role claude-rev  reviewer
& $py -m agenttalk roster set-role codex-rev   reviewer

& $py -m agenttalk roster set-group devs      claude-dev,codex-dev
& $py -m agenttalk roster set-group reviewers claude-rev,codex-rev
& $py -m agenttalk roster set-group team      claude-dev,codex-dev,claude-rev,codex-rev

& $py -m agenttalk roster set-operator-facing claude-lead
```

Notes that matter:

- **Roles are free-form strings.** Only `lead` carries code semantics: at most one
  lead exists (setting a second demotes the first in the same atomic write), and
  `role=lead` ∪ the operator-facing liaison form the **protected** set that the
  supervisor never auto-kills.
- **`set-group` defines full membership**, it is not additive.
- **`set-operator-facing` is advisory routing metadata, not an authorization
  boundary.** It makes `claude-lead` the target of `agenttalk escalate`, the
  strict authority for `request-launch`, and protected from auto-kill.
- `.agenttalk/` is gitignored — the bus is per-machine runtime state, and this
  laptop's bus is **fully independent** of the primary laptop's (work package §1).

### The cross-family review topology

| Agent | Family | Role | Reviewed by |
|---|---|---|---|
| `claude-dev` | Claude | builder | `codex-rev` |
| `codex-dev` | Codex | builder | `claude-rev` |

Every build gets a reviewer from the **other** model family. `claude-lead` is the
human-facing liaison and gates, but never counts as a reviewer.

---

## 5. Supervisor

```powershell
& $py -m agenttalk supervise --init      # scaffolds supervisor.json + 4 generated artifacts
```

Then edit `.agenttalk/supervisor.json` (the live config is the reference copy —
it carries `_comment_*` keys explaining each choice). The four supervised agents
are all **wrapped**; `claude-lead` is deliberately **absent** (see §6).

### Why each non-default value was chosen

**`stuck_after_seconds`.** Per-CLI, and not cosmetic.

- *Wrapped Claude* streams thinking/text/tool deltas **and** runs the in-turn
  `work_heartbeat` ticker (default-on for `--loop`), so it stays fresh through
  long turns. Code default 180s; set to **600s** here as headroom for `xhigh`
  reasoning.
- *Wrapped Codex* is **item-level** — nothing closes a pure-reasoning gap, so the
  stream is silent between turn start and the final message. Code default **2400s**.

> **⚠ Doc/code disagreement — do not follow the tutorial here.**
> `docs/supervisor-tutorial.md` recommends `1200`–`1800`s for wrapped Codex (and
> states the default is 900s). The code default is **2400s**
> (`supervisor.py:377`), and `_plan_one` (`supervisor.py:3749-3772`)
> **refuses restart-on-stale — degrading to warn-only — when
> `stuck_after_seconds <= turn_watchdog.turn_elapsed_seconds + 300`**. With the
> default `turn_elapsed_seconds=1800` the real floor is **2101s**. Following the
> tutorial's 1200s would silently disable stale recovery for that agent. The
> value is never silently coerced. Use 2400.

**`codex_home_isolation: true`** on both Codex agents. Two Codex agents share this
project directory; without a per-agent seeded `CODEX_HOME`, `resume --last` is
ambiguous between them.

> **⚠ Provenance warning.** A seeded `CODEX_HOME` **copies your operator base
> Codex config**, including MCP servers and tool definitions, then runs unattended
> with `approval_policy=never`. Any inherited MCP server executes headlessly with
> no human prompt, and an interactive/REPL-style entry can wedge a turn. agenttalk
> does **not** strip inherited config. Audit `~/.codex/config.toml` before enabling.

**`turn_watchdog`** (wrapped Codex, default-on). Two-factor fire — the turn has run
≥ `turn_elapsed_seconds` **and** a live non-codex tool descendant has been alive
≥ `tool_descendant_alive_seconds`. Known limitation: a *legitimately* long-running
tool inside a long turn will also be killed; raise both values if a role runs long
builds (and keep `stuck_after_seconds` above `turn_elapsed_seconds + 300`).

**`model` / `reasoning_effort`.** Resolve in three layers, highest first:
an explicit flag in the launch tail after `--` > `wrap --model/--effort` >
these per-agent keys. Valid efforts are a **closed per-CLI set** —
codex `{minimal,low,medium,high,xhigh}`, claude `{low,medium,high,xhigh,max}`;
invalid values are dropped with a warning, never a launch failure.

> **Changing either value resets that agent's session.** The wrapper fingerprints
> the *effective* value; on change it mints a new Claude session id / clears the
> Codex `thread_id`. Configure a stable profile per role and retune only on
> evidence.

Codex `model` is intentionally **unset**: Codex draws on a shared load-balanced
pool, so downgrading the model frees no capacity — vary effort, not model.

### Wrapped launch args — the shape that passes preflight

```json
"windows_args": ["-m", "agenttalk", "--root", "{ROOT}", "wrap", "--for", "<AGENT>",
                 "--cli", "claude|codex", "--loop", "--", "<REAL exe>", "<base args>"]
```

- `windows_file` is the **Python** exe, not the CLI exe.
- `--root {ROOT}` **must appear before the `wrap` token** or `bootstrap-check`
  errors `supervisor_wrapped_missing_root`. (The executor auto-injects it at
  launch for legacy configs, so a missing `--root` still *runs* but fails
  preflight — put it in explicitly.)
- **No `{SESSION_ARGS}` token** — the wrapper owns session continuity end to end,
  across its own turns *and* across a supervisor relaunch.
- Do **not** put `-p`, `--output-format`, `--session-id`, `--resume`, `exec`, or
  `--json` in the tail; the wrapper appends per-turn args itself.
- `--permission-mode bypassPermissions` is auto-injected for Claude from
  `claude_permission_mode`, so it is optional in the tail.
- `--disable hooks` on the wrapped Codex tail is the safe default: the wrapper
  owns the heartbeat, so the Codex activity hook is unwanted and disabling it
  sidesteps the hook-trust prompt on every launch.

---

## 6. Why `claude-lead` is not supervised

`claude-lead` is the human-facing liaison and runs as an **interactive Claude Code
window**. It is absent from `supervisor.json` on purpose:

- **One window per agent.** Two consumers on one mailbox is unsupported. A
  supervised `claude-lead` would race the interactive one.
- The managed **lead-loop** (`wrap --loop --lead-loop`) exists for a *hands-off*
  lead that should own its mailbox via a lease. That is the opposite of what a
  human-facing window wants.

The liaison instead gets a Claude-only heartbeat hook with an explicit fallback
identity, so its liveness is honest even in a window without `AGENTTALK_SELF`:

```powershell
& $py -m agenttalk supervise --install-activity-hook --interactive-for claude-lead
```

`bootstrap-check` will report `operator_facing_not_fresh` as a **warn** whenever
that window is closed. That is expected and non-blocking.

---

## 7. Launch and verify

```powershell
$pwsh = (Get-Content .\.agenttalk\powershell-host.json -Raw | ConvertFrom-Json).path
New-Item -ItemType Directory -Force -Path .\.agenttalk\logs | Out-Null

Start-Process -FilePath $pwsh `
  -ArgumentList '-NoLogo','-NoProfile','-NonInteractive','-File','C:\Projects\agenttalk\.agenttalk\supervisor.ps1' `
  -WorkingDirectory 'C:\Projects\agenttalk' `
  -RedirectStandardOutput '.\.agenttalk\logs\supervisor.out.log' `
  -RedirectStandardError  '.\.agenttalk\logs\supervisor.err.log' `
  -WindowStyle Hidden -PassThru
```

Redirecting to a log is work-package §8.5: **a bad launch must be visible, not
hidden.** A silently mis-wired agent looks "idle", not broken.

Verification ladder — run in order, each is read-only:

```powershell
& $py -m agenttalk supervise --bootstrap-check | ConvertFrom-Json | Select-Object verdict,summary
& $py -m agenttalk supervise --report     # per-agent fresh/stale + resolved threshold
& $py -m agenttalk supervise --plan       # the decision table the monitor executes
& $py -m agenttalk doctor
& $py -m agenttalk status
```

**Healthy looks like:** `bootstrap-check` verdict `ok` (or `warn` only from
`operator_facing_not_fresh` / `roster_identity_not_live`); `--report` shows
`heartbeat_stale=false` for all four wrapped agents; `--plan` shows no action /
`HEALTHY_IDLE` for each, with no `READINESS_GAVE_UP` and no restart storm.

Before launch the four `supervisor_agent_not_fresh` **errors are expected** — the
agents have not started yet. They clear on first heartbeat.

### The end-to-end proof

Config that validates is not a team that works. The real check is a **round trip**:
send a question and get a bus reply back.

```powershell
& $py -m agenttalk send --from claude-lead --to claude-dev --kind question `
  --subject "wiring check" -m "Reply with your resolved agenttalk root and version."
& $py -m agenttalk wait --for claude-lead --to-request <printed request id>
```

If an agent runs turns but its bus commands silently fail, the env is mis-wired —
that is the §8.5 failure mode, and it presents as "idle/stalled", not as an error.

---

## 8. Operating the team

| Need | Command |
|---|---|
| Dispatch owned work | `send --from claude-lead --to <agent> --kind question --meta assignment=<id>` |
| Ask the whole team | `broadcast --from claude-lead --to-role reviewer --kind question` |
| Wait on one thread | `wait --for claude-lead --to-request <rid>` |
| See open obligations | `sync --for claude-lead` · `threads --for claude-lead` |
| Bounce an agent (keeps context) | `request-restart --for <agent> --reason "..."` |
| Fresh-eyes evidence review | `request-launch --from claude-lead --profile codex-evidence-reviewer --skill review-code --revision <full 40-char SHA> -m "..."` |
| Relay a human stand-down | `release --from claude-lead --to <agent> --relay-human -m "<the human's decision>"` |
| Stop supervising | Stop `supervisor.ps1` (kill the pid in `.agenttalk/logs/supervisor.pid`) |

Discipline that is easy to get wrong:

- **Prose never stands anyone down.** "done for now" / "stand by" leaves a listener
  listening. Only a `kind=release`/`end` with a valid authority marker stops one,
  and the lead only ever *relays* a human decision (`--relay-human`).
- **Idle means keep listening** — just stop sending work.
- **A fresh (ephemeral) reviewer's approval is evidence only**, never a counted
  signoff. A fresh *rejection* is a counter to disposition.
- **Rescind, don't retract in prose.** `agenttalk rescind --to-request <rid>` moves
  thread state; "ignore my last message" does not.

**Backing out of supervision** is just stopping `supervisor.ps1`. The bus —
messages, roster, cursors, threads, sessions — is never touched by turning
supervision on or off.

---

## 9. Known gaps on this host

**1. ~~The dual-version gate cannot be run here.~~ RESOLVED.** Work package §7
requires `ruff + bandit + pytest` on Python **3.10 AND 3.14**. This host shipped
only 3.14.6, which would have blocked calling D2–D4 done.

Resolved with a **portable Python 3.10.11** — the NuGet CPython distribution, which
is a full interpreter with the complete stdlib, unlike the embeddable zip. No admin,
no registry, no PATH change; remove by deleting the folder.

```powershell
Invoke-WebRequest "https://www.nuget.org/api/v2/package/python/3.10.11" -OutFile python310.nupkg -UseBasicParsing
Expand-Archive python310.nupkg -DestinationPath C:\Tools\Python310 -Force
(Get-AuthenticodeSignature C:\Tools\Python310\tools\python.exe).Status   # Valid

$py310 = "C:\Tools\Python310\tools\python.exe"
& $py310 -m ensurepip --upgrade
& $py310 -m pip install "pytest>=8.0" "ruff>=0.6" "bandit>=1.7"
```

Verified working against this repo (`PYTHONPATH=<repo>\src`):
`ruff check src/` → all checks passed; `pytest -k "gates or close or lanes"` →
**372 passed** in 204s on 3.10.11.

The dual-version gate is therefore:

```powershell
$env:PYTHONPATH = "C:\Projects\agenttalk\src"
foreach ($p in @("C:\Tools\Python310\tools\python.exe",
                 "C:\Users\milos\AppData\Local\Programs\Python\Python314\python.exe")) {
  & $p -m ruff check src/
  & $p -m bandit -r src -x src/agenttalk/skills -q
  & $p -m pytest tests/ -q --basetemp="$env:TEMP\at-gate"
}
git diff --check
```

Standing rule regardless: never report a single-version local run as a passed
dual-version gate. Under `ASSURANCE.md` that is a referenced-not-executed claim,
and a green-but-skipped leg is a HOLD, not a GO.

**2. Reviewer floor.** Work package §7 says "≥2 cross-family reviewers".
`docs/ASSURANCE.md:93-96` sets a **hard floor of 3** independent reviewers for a
Tier-3 *design* panel, with no designer/builder/lead counted and ephemeral reviews
explicitly not satisfying the minimum. A native work/evidence spine is Tier 3
(authority/gate, provenance/integrity, fail-open semantics, durability and
persistent-state contracts). The stricter bar governs — resolve this with the
operator before the review round, not at merge.

**3. Skill currency.** `doctor` reports 35 advisory skill-currency issues (bundled
skills stamped against 0.43–0.75 vs. package 0.78). Advisory, zero blocking — but
it means a skill body may describe an older CLI surface than the one installed.
Verify a flag against `--help` before relying on it.
