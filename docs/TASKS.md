# Task index

> **What this is.** The lead agent tracks work as numbered tasks and cites them as `#NN` in dispatches,
> commit messages, reviews and status reports. That numbering previously lived only in the lead's
> session, so an operator reading "#121" had no way to look it up. This file is the index.
>
> **Status legend:** `[x]` completed · `[~]` in progress · `[ ]` pending.
> Numbering starts at #9 — earlier items predate the tracker.
>
> Companion documents: `docs/ROADMAP.md` (§6.0 is the per-release plan — which of these ship when),
> `docs/logbook/` (what actually happened, day by day, and why).
>
> Last generated: 2026-07-30.

## Currently active

These are the ones being worked right now, and the ones most likely to be cited at you.

| # | What it is |
|---|---|
| ~112 | **The keystone defect.** The per-turn watchdog's own kill wedged the wrapper it was protecting: the turn never failed, the wrapper hung forever at `phase=active`. Ten instances measured today, all within ~47s of 1800s. **Fixed and SHIPPED in v0.80.0** (`e0faf96`, gated 14/14 on the post-bump commit). |
| ~121 | **Running the dev-gate inside a wrapped turn guarantees a watchdog kill.** The watchdog fires at 1800s elapsed AND 600s of a live tool descendant; the gate's pytest leg is allowed 2700s. Gating in-turn satisfies both conditions by doing the job correctly. Fix is a detached runner, not higher thresholds. |
| ~122 | **Head-of-queue livelock.** A wrapped agent processes one message per turn, so a message whose processing wedges the wrapper blocks the instruction that would fix it. Recurred today: rescinding the head just promotes the next message into the same trap. |
| ~87 | P0 supervisor recovery authority (Design 87-A). Round-3 delta panel returned 4 blockers + 4 majors; obligations tracked in `docs/rr87a-delta-panel-obligations.md`. |
| ~105 | Team Console and doctor rendered raw wrapper health as green, so a dead CLI child looked healthy. **Fixed, green 13/13 on `a5171fc`.** |
| ~115 | Design 87 assumed a supervisor-state lock that did not exist — state read-modify-write was unserialised. Published at `bcc3666`. |
| ~120 | Register the whole owned process tree per agent, not just wrapper+launcher PIDs — the thing that wedges is the tool descendant. Published at `28f663f`. |
| 126 | A provider usage-limit refusal is classified `ambiguous_or_unknown`, so it retried 20 times against a hard wall with a known reset date. Measured 80 wasted turn-starts in one outage. |
| 127 | Parallelise the pytest legs. 96.2% of the Windows gate is pytest; this is the only change that cuts latency rather than queue contention. |
| ~125 | CI had no concurrency group, so every push queued an additional full matrix instead of superseding. **Fixed, merged `99668d9`.** Note the trade-off discovered afterwards: `cancel-in-progress:false` on master means master runs now SERIALIZE (queue), not run in parallel. |
| 56 | Wrapped ovh-qwen env allowlist strips `HOME`/`LOCALAPPDATA`, so `agenttalk reply` crashes in the child shell. The gate on Qwen becoming a usable third provider. |
| ~11 | Qwen-on-OVH as a first-class wrapped team member (design → PoC → trial). |
| 131 | **Launch-provenance authorization for the checked observer.** The ancestry check proves selected-host/image ancestry, not execution of `supervisor.ps1` — so a caller supplying the same hidden identity args can mint a trusted `startup` observation. An existing test *asserts* the permissive behaviour. Design-first; **PR 107 (#114) is held on this.** |
| 132 | **Cold diff review as a gate.** `agenttalk request-launch` already spawns a fresh SHA-bound adversarial reviewer, but `ephemeral_reviewers` is `enabled:false` and unconfigured. Delta: line-anchored typed findings, a PR renderer, two vendor profiles, and a merge gate bound to the head SHA. Scored 2026-07-31 against a real baseline — see the logbook. |

## Full list

| # | S | Title |
|---|---|---|
| 9 | x | PowerShell baseline enforcement (Core 7.0+; 5.1 unsupported) — v0.78.0 |
| 10 | x | Codify tiered adversarial design review + docs testing into the cadence |
| 11 | ~ | Qwen-on-OVH as first-class wrapped team member (design→PoC→trial) |
| 12 |   | Agent worklog + staleness→nudge self-healing (L0/L1/L2) |
| 13 |   | Harden `gate waive` to an authenticated operator origin |
| 14 |   | Wire the one-source cadence references (DESIGN.md pointer + lead-skill step) |
| 15 |   | Evaluate Qwen 3.6-27B as an alternative wrapped worker |
| 16 |   | Per-agent fresh-session + codex-rollout size bound (context-bloat host crashes) |
| 17 |   | Validate/normalize `request_id` at write time (bus + knowledge) |
| 18 |   | Diagnosis-discipline preflight — superseded in principle by #76 |
| 19 | x | Supervisor crashed when a new agent was added to a running supervisor |
| 20 |   | Warm-session mode (keep the wrapped CLI alive across turns) |
| 21 | x | Supervisor daemon crashed on `File.Replace` atomic-write contention |
| 22 |   | Make the lead/human-facing inbox unmissable (read-path hardening) |
| 23 | x | Regenerate + reconcile + relaunch the supervisor (operator-attended) |
| 24 |   | Supervisor crash-simulation harness — merge into #68's chaos simulator |
| 25 |   | Cross-platform supervisor (POSIX path) |
| 26 | x | doctor: warn when a gated external-worker agent has no commit-gate policy |
| 27 |   | Coordinate the independent 2nd-laptop team (Native Work & Evidence Spine) |
| 28 |   | `stuck_after_seconds` docs recommend below the watchdog-preempt floor |
| 29 |   | Supervisor misclassifies a never-launched agent as STUCK_OR_DEAD |
| 30 |   | `supervise --select-pwsh` re-probes instead of returning the recorded host |
| 31 |   | Close-provenance envelope in `close.py` |
| 32 |   | `_atomic.write_text` latched Windows sandbox-direct fallback is not crash-atomic |
| 33 |   | Seeded CODEX_HOME inherits operator MCP config unstripped, `approval_policy=never` |
| 34 | x | Enforcement canary: deterministic stub-agent end-to-end validation |
| 35 | x | Enforcement was not cross-platform — 107 tests failed on macOS + Ubuntu |
| 36 |   | Agent-lifecycle RFC: ephemeral + bounded-warm + supervisor dispatch |
| 37 | x | Store wedged all bus writes on an incomplete publication-order sidecar |
| 38 | x | Gateway-compat fold: committed source regressed vs the live process |
| 39 |   | Plan-and-estimate gate for external/untrusted workers (two-phase dispatch) |
| 40 | x | `drain \| head` silently consumed undisplayed mail — no paging guard |
| 41 | x | deadman config in `supervisor.json` was silently inert |
| 42 |   | CLI command-registration seam so domains attach subcommands without editing cli.py |
| 43 | x | `domains.check_path` accepted a glob silently |
| 44 |   | `note_id` is mintable and validatable but never resolvable |
| 45 | x | Cross-laptop lead-to-lead chat (Drive drop-folder) |
| 46 | x | Store full-validation snapshot + ordered-but-absent detection |
| 47 |   | review-result status vocabulary + schema-tightening silent drop |
| 48 |   | No honest closure for an overtaken review-request |
| 49 |   | Docstring O(n)-under-send-lock + lock the stable-code contract with tests |
| 50 | x | Hermetic SHA-bound gate command (`agenttalk dev-gate`) |
| 51 |   | RFC: Substrate v2 — immutable signed event log + fact plane |
| 52 |   | Per-thread problem attribution for validated messages |
| 53 |   | Cross-machine lead↔lead courier — parked behind Qwen |
| 54 | x | dev-gate 1800s per-leg cap too tight for the Windows wheel pytest leg |
| 55 |   | Code-comprehension plane for migrations (validate Graphify as prior art) |
| 56 |   | **Wrapped ovh-qwen env allowlist strips HOME/LOCALAPPDATA → `reply` crashes** |
| 57 |   | Enforce a project-level singleton per wrapped agent (launch lock) |
| 58 | x | Wrapped worker wedged carrying accumulated failed-attempt baggage |
| 59 | x | Harden the 4 flaky Windows-CI tests that tax every merge |
| 60 | ~ | DESIGN: red-by-default until evidence exists — assurance forcing gate |
| 61 |   | SC Papendal → acceptance-server milestone |
| 62 | x | SAFE config-blocked auto-recovery (non-disposing re-probe) |
| 63 | x | Gateway leaked child-turn reservations → per-child budget exhaustion |
| 64 |   | Authenticated CI attestation for gate `evidence_source` |
| 65 |   | Authenticated operator escape for DoD dimensions |
| 66 |   | DoD resolution↔close-commit TOCTOU |
| 67 | ~ | CI determinism: pin/vendor the security stack; split real findings from flakes |
| 68 |   | Scenario-fleet chaos simulator (stub agents, no model spend) |
| 69 |   | Supervised headless Claude worker — propagate Claude auth into the child |
| 70 |   | Coverage producer: robust stale-gate invalidation on CLI-parse failure |
| 71 | ~ | Checkpoint-before-compact for interactive + wrapped agents |
| 72 | x | Supervisor health must verify the live CLI child, not just the wrapper |
| 73 | x | Wrapper false-park: completed work not committed/replied |
| 74 |   | Wall-clock/flake family, 19 instances. `test_ovh_gateway_front` is the dominant merge tax |
| 75 |   | Checkpoint bus budget reports a false `unread:0` on a real store |
| 76 |   | Define + gate the earned-green invariant — close the CHANNEL CLASS, not the instance |
| 77 |   | Fleet cost visibility: per-agent turns, context, model/effort in `supervise --report` |
| 78 |   | Recovery cannot terminate a LIVE orphaned wrapper (the barrier is correct) |
| 79 | x | Lens-partitioned review round on the decide-done region |
| 80 |   | Resume failures aren't counted, so a deterministic resume error retries forever |
| 81 |   | Define + gate the recovery-actually-recovers invariant (umbrella) |
| 82 |   | Bind `order_reconstructed` provenance into the publication-order tamper anchor |
| 83 |   | A live per-session context source exists — wire `capacity_refresh` to it |
| 84 |   | Ratchet the zero-runtime-dependency property in CI |
| 85 |   | Score the previous release's ledger claims at each release |
| 86 |   | `auto_restart_protected` is inert AND the scaffold tells operators to set it |
| 87 | ~ | **P0 supervisor recovery authority (Design 87-A)** — see the active table above |
| 88 |   | A gate leg killed by the job timeout writes NO evidence artifact |
| 89 |   | Carry 3 enforcement-canary scenarios into `tests/test_stub_agent_canary.py` |
| 90 |   | 21 design/review artifacts exist only as untracked files on one laptop |
| 91 | ~ | The durable-order guard was applied to ONE consumer; two more read best-effort |
| 92 |   | Finding set: Lens C F3–F7 — rows for the #76/#81 allowlists |
| 93 | ~ | Two runtime bindings: pin the TOOL to a release AND bind code-under-test to its checkout |
| 94 |   | Release plan: one theme per release, max 4 items, umbrellas before instances |
| 95 |   | Transcript-derived context/burn needs no statusline; only rate-limit % does |
| 96 |   | The generated `bin/agenttalk.cmd` shim is unsound twice over |
| 97 |   | Nothing reaps agent worktrees — 88 on this host |
| 98 |   | An escalation resolved by later success is never retracted |
| 99 |   | An EXPIRED coverage waiver leaves the coverage DoD on HOLD forever |
| 100 | x | Dual-shell PS test silently dropped a shell when absent |
| 101 |   | Tool residue not in `.gitignore` makes clean-worktree checks noisy |
| 102 |   | Pinned-runtime Python guard as a channel class: every launch path |
| 103 |   | doctor probes a different executable/environment than the pinned launch |
| 104 |   | Resume lifecycle observability: no per-attempt event, no success record |
| 105 | ~ | **Team Console and doctor rendered raw wrapper health as green** |
| 106 |   | Coverage subprocess output captured unbounded before the 16 MiB bound applies |
| 107 |   | One owned-subprocess helper + a ban on raw `subprocess.run` |
| 108 |   | Nothing provisions the `.agenttalk/` ignore rule that ASSURANCE.md asserts |
| 109 |   | No instrument abandons a message that retries forever |
| 110 |   | Wrappers launch Hidden with no stdout/stderr redirection, so tracebacks are destroyed |
| 111 |   | Expose a public validator for the lock-generation format |
| 112 | ~ | **The per-turn watchdog's own kill wedges the wrapper it was protecting** |
| 113 |   | The project had no logbook — narrative lived in the lead's private memory |
| 114 | x | Kill-switch present at STARTUP produced zero persistence |
| 115 | ~ | Design 87 assumed a supervisor-state lock that does not exist |
| 116 |   | Absence is not staleness: relaunch a twice-confirmed-absent wrapper immediately |
| 117 |   | Capture wrapper stdout/stderr to bounded per-agent log files |
| 118 |   | All same-family wrapped agents share ONE memory store, keyed by cwd not identity |
| 119 |   | The lead's memory store is oversized because ONE file is an append-only log |
| 120 | ~ | **Register the whole owned process TREE per agent** |
| 121 | ~ | **Running the dev-gate inside a wrapped turn guarantees a watchdog kill** |
| 122 | ~ | **Head-of-queue livelock** |
| 123 |   | A newly-added agent NEVER auto-launches (lands in CLI_CHILD_UNKNOWN) |
| 124 |   | `cli.py status` reads supervisor-snapshot without a freshness gate |
| 125 |   | CI has no concurrency group, so every push queues a full superseded matrix |
| 126 |   | A provider usage-limit refusal is classified `ambiguous_or_unknown` |
| 127 |   | Parallelise the pytest legs — 96.2% of the Windows gate is pytest |
| 128 |   | Build releases in CI, not on a laptop (re-gate the post-bump commit, publish provenance) |
| 129 |   | A refused restart-request is never retired, so it retries every tick forever |
| 130 |   | `web.py /api/attention` has a private source allowlist that SILENTLY DROPS unlisted sources |
| 131 |   | **Launch-provenance authorization for the checked observer** — prerequisite for #114 |
| 132 |   | **Cold diff review as a gate** — ephemeral reviewer bound to a PR head, findings line-anchored |
| 133 |   | Runtime-observation quarantine files are not status-indexed and have no retention cap |

## A caveat worth stating

Two numbering spaces collide in conversation and it is genuinely confusing:

- **`#NN` in this file** is a lead task.
- **`PR NNN` / `#NNN` on GitHub** is a pull request.

They overlap. "#103" is both *doctor probes a different executable than the pinned launch* (task) and
*the CI concurrency PR* (GitHub). Where it matters, the intended convention is "task #103" versus
"PR 103". Ask if a citation is ambiguous — the collision is real, not something you are misreading.
