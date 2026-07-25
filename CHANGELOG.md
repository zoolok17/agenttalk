# Changelog

All notable changes to agenttalk are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.79.0] - 2026-07-24

### Added

- **Checkpoint-before-compact (`agenttalk checkpoint save|resume|show`).** A native, fail-soft
  hook command captures a durable per-agent checkpoint (git HEAD/branch/dirty, context-window fill,
  and the agent's owed/in-flight bus state) before a manual or automatic context compaction, and
  re-injects a reload pointer on resume. `save --hook` always exits 0 so it can never block a
  compaction; `resume --hook` emits exactly one SessionStart envelope. (#71)
- **`supervise --install-activity-hook` now wires the checkpoint hooks too.** The same operator
  action that installs the Claude heartbeat `PostToolUse` hook also installs `PreCompact`
  (checkpoint save) and `SessionStart`/`compact` (checkpoint resume), reporting per-hook
  `installed` / `already` / `skipped (reason)` so a partial or malformed install can never be
  presented as blanket success. A managed entry whose `type` is missing or wrong is repaired
  rather than reported as already-installed. The installed commands carry a fail-soft fallback to
  the silent heartbeat hook, so an `agenttalk` that predates the `checkpoint` subcommand but is
  otherwise recent enough exits 0 instead of blocking a compaction. **Bounded legacy-PATH
  residual:** the neutral fallback needs roughly v0.31.1 and the `--fallback-for` form needs
  v0.69.6, so an OLDER executable selected first on `PATH` can still return exit 2 and block a
  compact — upgrade or correct `PATH` before installing these hooks (documented at
  `docs/USER-MANUAL.md`). `--codex` / `.codex/hooks.json` remains heartbeat-only. (#71)
- **"Red-by-default until evidence exists" DoD forcing-gate.** `close` now supports pluggable
  Definition-of-Done dimensions that HOLD until independent, revision-bound evidence exists — the
  assurance dimension (inc-1) and the knowledge dimension (inc-2). (#60)
- **First-class OVH-Qwen wrapped-worker gateway.** The gateway is folded onto master (ending the
  committed-vs-live divergence), with a pinned model endpoint, ledger-preserving
  `gateway reconfigure`, per-child turn caps, and fail-closed spend safety. (#38, #11)
- **Detection-grade owed-action enforcement for wrapped workers.** A durable obligations engine
  makes delivery failures requester-terminal and resists policy drift across commit boundaries.
  (#32, #16)
- **Read-side `request_id` / `broadcast_id` / correlation resolvers.** (#17)
- **Store full-validation snapshot + ordered-but-absent detection.** (#44)
- **A committed, SHA-bound `agenttalk dev-gate` replaces hand-run test and security rituals.** The release
  profile runs isolated source and built-wheel tests, packaging contracts, Ruff, Bandit, gitleaks, pip-audit,
  Semgrep, and zizmor; emits strict normalized JSON evidence; and fails closed on missing tools or fields.
  CI produces incomplete evidence for each declared OS/Python leg and an authoritative aggregate only after
  all 12 legs prove the same candidate and manifest binding. CodeQL is the sole explicit CI-native exception.

### Fixed

- **A short downstream reader can no longer make `drain` or `recv --ack`
  silently consume undisplayed mail.** Consuming output is written and flushed
  one record at a time, and cursor commits stop at the last confirmed record on
  output failure. Unbounded output to a positively identified pipe is refused;
  use the new `-n` / `--limit` option for bounded pages. Regular-file redirects
  remain allowed, and JSON receive now uses one snapshot for output and commits.
- **A config-blocked wrapped-worker park now writes a visible health state** instead of a frozen
  "idle", so a parked agent is diagnosable rather than silently stuck. (#58)
- **A transient gateway hold re-drives the parked turn and self-heals when the hold clears** instead
  of dead-lettering it; the gateway refuses to mint a child turn while held. (#62, #63)
- **`deadman` config in `supervisor.json` is honored** (the reader previously used only
  `.agenttalk/config.json`). (#41, GH#35)
- **`domains.check_path` rejects a glob descriptor** instead of silently answering point-membership.
  (#43, GH#36)
- **The publication-order sidecar self-heals under writer skew** rather than wedging all bus writes.
  (#37, GH#37)
- **Supervisor hardening:** hot-added agents no longer crash a running supervisor; relaunch is
  reserved before spawn; state-swap and atomic-write contention are tolerated. (#19, #21)
- **Wrapped-Qwen child fixes:** the exact `agenttalk reply` invocation is provided, the child home
  vars are scoped so `reply` resolves, and the reasoning-model output cap is raised. (#11, #38, #56)
- **De-flaked the four timing/lock/socket Windows-CI tests** that taxed every merge, and widened
  the Windows dev-gate leg timeout + the shared pytest wheel-leg cap. (#59, #67, #54)
- **De-flaked the config-lock acquisition budgets that red-blocked merges under CI load.** A
  concurrency property test was unintentionally inheriting the product `_config_lock()` default
  instead of a test budget, and two marker-recovery tests used sub-second wall-clock ceilings.
  Tests only — every assertion, the 24-writer concurrency, and the sub-second budgets on the
  negative/timeout cases (where the budget *is* the property) are unchanged, and the product
  default is untouched. (#59)

## [0.78.1] - 2026-07-15

### Fixed

- **The cross-platform test matrix now guards only Windows-specific PowerShell
  host cases.** Windows path, process, selection, and Scheduled Task assertions
  skip on Linux and macOS, while portable policy, fingerprint, artifact marker,
  generation, parsing, and diagnostic tests continue to run on every platform.
- Portable supervisor tests now tolerate unavailable process-start tokens on
  macOS, and web error-log assertions no longer depend on Python's traceback
  header format.

## [0.78.0] - 2026-07-15

### Added

- **One durable PowerShell Core host selection for every Windows supervisor
  boundary.** `supervise --select-pwsh` probes only trusted Program Files
  candidates unless the operator supplies an absolute `--pwsh`; explicit and
  current-host selections are terminal and never fall through. The selected
  canonical path, discrete version, native file identity, 24-hour probe time,
  monotonic revision, and deterministic fingerprint are stored atomically in
  `.agenttalk/powershell-host.json`. PATH and Scheduled Task action paths are
  data only and are never probed or auto-executed.
- **Claim-time host consistency and detectable generated artifacts.** A generated
  supervisor claim independently checks the live PowerShell ancestor, native
  image identity, start time, direct/cmd-hop ancestry, and current selection while
  holding lifecycle -> selection -> config locks. All three generated `.ps1`
  files plus `bin/agenttalk.cmd` carry one deterministic schema/generation marker;
  covered entry points reject missing, stale, mixed, or edited files with the
  exact `agenttalk supervise --refresh-scripts` remediation.
- **PowerShell diagnostics and recovery controls.** `doctor`, human/JSON `status`,
  `start`, the Scheduled Task helper, and the Windows watchdog now consume the
  selected absolute host. `supervise --repair-instance-marker --quarantine
  --acknowledge-no-live-supervisor` is the explicit corrupt-marker recovery path.

### Changed

- **PowerShell Core 7+ is the supported Windows supervisor baseline; Windows
  PowerShell 5.1 is refused.** Stable 7.0-7.3 remains accepted with an end-of-life
  warning, every prerelease warns, and stable 7.4+ is recommended and quiet.
  Generated scripts use `#requires -Version 7` and `#requires -PSEdition Core`
  plus an in-process edition/major guard.
- **The real launch sites no longer use a bare shell name.** `agenttalk start`,
  Scheduled Task registration, and both watchdog CIM probes use the validated
  selected path. Watchdog selection or revalidation failure remains fail-open:
  no snapshot means no kill.
- **Generated PowerShell and CMD artifacts are BOM-free.** Existing
  `utf-8-sig` readers remain in place for legacy, operator-authored, and
  PowerShell-written inputs. `supervise --init --force` and
  `--refresh-scripts` preserve an existing `supervisor.json` byte-for-byte and
  do not touch runtime state.

### Known limitations

- Generated artifact replacement is deliberately per-file, not group-atomic;
  marker/content validation detects a partial set and a refresh rerun converges.
  A same-selected-Core process that already parsed old script bytes but has not
  claimed remains a narrow launcher-mutex race. Task rebinding/multi-binding
  migration and executable signer/ACL attestation remain deferred.

## [0.77.0] - 2026-07-15

### Added

- **Explicit coordination stalls are loud without treating healthy idle as
  deadlock.** Wrapped consult/handoff sends can record a generation-bound
  `--await-reply` edge; manual scoped waits remain compatible. A pure detector
  warns only for a supervisor-confirmed unavailable target or a persistent manual
  restart barrier, after two matching polls, and projects one stable advisory to
  attention, doctor, status, and Team Console. Generic idle, ordinary outbound
  requests, uncertain manual agents, and short waits remain silent. The detector
  never kills, restarts, releases, reroutes, moves a cursor, sends a message, or
  changes a gate; global all-idle/cycle inference remains deferred.

## [0.76.1] - 2026-07-15

### Fixed

- **Knowledge notes and lessons are visible from the default retrieval surfaces.**
  `knowledge pull`, `search`, and `onboard` now select curated pointer notes and
  accepted lessons through one mixed-view pipeline; scope/tag filters include lessons
  without requiring `--type lesson`, and JSON callers receive a versioned
  `knowledge-view-v1` envelope. Explicit `--type` output stays compatible, while
  `--output-schema legacy` preserves the old pointer-only JSON array.
- **Registry edits no longer hard-stale unrelated knowledge.** New events bind the
  normalized definition of their effective domain. A change to that domain requires
  re-verification; an unrelated registry change is a caution. Historical events without
  the scoped hash remain curated and visible with a legacy-freshness caution.
- **Curation is causally bound and publish validation is complete in one pass.** New
  curate/retract events must name the current prior event and hash its complete
  validated content, including publisher attribution and creation time, inherited
  supersession lineage, lesson ownership, and lesson supersession declarations;
  non-causal rows are skipped without hiding valid history or reopening tombstones.
  Modern events reject unknown persisted fields, while historical rows are
  canonicalized before output. Modern lesson curation also requires its nested curator
  to match the top-level curation actor. Curation rechecks the registry against
  supported locked writers before append; an out-of-band edit is caught fail-closed
  by scoped freshness.
  Publish reports all independent field errors before registry, Git, or ledger I/O.

## [0.76.0] - 2026-07-15

### Added

- **Team Console — plain-language, at-a-glance legibility for a zero-context (C0) viewer.**
  The dashboard now answers "is the team OK, and does anything need me?" in words, not just
  colour — for a non-technical viewer glancing at the screen.
  - A single **team-health verdict** in the top-bar pill and the overview subtitle
    ("All N agents healthy — nothing needs you", "N need a human", "reconnecting…"…). It is
    **honest under failure**: a green all-clear shows ONLY when both the agent-health feed
    (`/api/state`) and the human-attention feed (`/api/attention`) are fresh, known, and
    empty. A loading / failed / stale / error-as-data feed reads as "reconnecting" /
    "status unavailable" / "queue status unknown" — never a false "nothing needs you". A
    known non-empty human queue is always surfaced as urgent even when the state feed is
    stale or degraded, and the verdict re-evaluates once per second so a green pill flips to
    "reconnecting…" in place when a poll outage ages the data out.
  - **Plain-language tooltips** on every status / kind / verdict / severity / source chip,
    the heartbeat and rate/context meters, the supervised and CLI badges, and the sidebar
    legend (now a glossary). Every signal shows the WORD plus colour — never colour alone.
  - Absolute-time hover on every relative age; attention-needing agents sort first in the
    overview grid; a real "No agents running yet" empty state; an activity-rail
    "last message Xm ago" summary; and an errored / stale attention or state feed surfaces
    the failure ("status unknown" / "out of date" / last-known banner) in its own view
    instead of a false "All clear".

## [0.75.3] - 2026-07-14

### Fixed

- **BOM defense-in-depth across the PowerShell↔Python file boundary** (from an exhaustive
  audit prompted by the 2026-07-14 orbit-launcher incident; the "all CLIs crashed" symptom
  itself was a Windows Terminal crash, **not** agenttalk). Under **Windows PowerShell 5.1**,
  `Set-Content -Encoding utf8` writes a UTF-8 BOM that a strict Python reader rejects and a
  TOML section scan mishandles.
  - **Live bug:** the per-agent codex-home `config.toml` empty-seed wrote a BOM-only file
    under 5.1, which skewed the `[projects]` section scan into **duplicate tables → invalid
    TOML the codex CLI refuses → the wrapped Codex agent couldn't start**. The two PowerShell
    JSON/TOML writers (`Get-ProcSnapshot`, the config.toml seed) now write **BOM-free** via
    `WriteAllText(UTF8Encoding($false))`.
  - **Self-repair:** the launch seed (`supervise --seed-codex-config`) and `codex-config
    --enable`/`--disable` now **collapse duplicate `[projects]` tables** (a semantic,
    project-scoped match, so an operator's `[projects."x"]` and agenttalk's `[projects.'x']`
    for the same path are recognized as one), and `codex-config --status` + `doctor` **warn**
    on a duplicated config instead of reporting it healthy — so an already-corrupted machine
    heals on the next launch.
  - **BOM-tolerant reads:** every reader of an operator-authored or PowerShell-written config
    now decodes with `utf-8-sig` — supervisor `settings.json`/`hooks.json` (a BOM no longer
    silently skips the merge), `config.toml`, `supervisor-state.json` (the one strict outlier),
    `domains.json`, and `signoffs.json`. (agenttalk's own atomically-written artifacts are
    BOM-free by construction and unchanged.) See decision **D-26**.
- Docs: prefer the **pwsh 7** host, don't host the whole fleet in one Windows Terminal
  (single point of failure), and the self-matching-`CommandLine` process-forensics gotcha.

## [0.75.2] - 2026-07-14

### Changed

- **Governance policy encoded into the durable docs and skills** (docs + skills only, no
  code): how agents and operators pick a model + reasoning-effort, keep to the live roster,
  and manage session context.
  - `AGENT-MANUAL.md` §1 gains: *the live roster is authoritative* (never dispatch from a
    memorized/handed-off roster; `sync` on a manual rejoin, read-only `roster`/`whoami`
    inside a wrapped turn); a per-task-class **model/effort selection** table with
    evidence-based escalation and provider asymmetry (Codex = shared load-balanced pool →
    cap/stagger, vary effort not model; Claude = weekly budget → sonnet workhorse, reserve
    opus); and a **context lifecycle** section (when to reset a session vs. spin up a
    one-off, preferably different-model-family, independent reviewer). The runtime-source
    precedence is stated as three layers — explicit child tail after `--` > `wrap
    --model`/`--effort` > `supervisor.json` — and the honest ceiling that there is **no
    bus context-reset command** (`request-restart` preserves the session).
  - `DESIGN.md` decision **D-25**: model/effort selection and agent-context lifecycle are
    operator/project policy, not core mechanism (the core only injects + fingerprints per
    D-24). Stale `D-1..D-15` decision-log cross-references corrected to `D-1..D-25`.
  - `USER-MANUAL.md` §8: an operator model/effort config rubric + how to verify the flags
    reached the CLI (idle-until-dispatch; inspect the `claude.exe`/`codex.exe` child, whose
    spelling differs by provider); §4 roster common-mistake.
  - Skills: Claude + Codex lead and listen skills and `devkit/_shared/references/routing.md`
    carry the matching rules; `reviewed-against` re-stamped `0.75` on the five touched skills.

## [0.75.1] - 2026-07-14

### Added

- **Runtime identity on the Team Console.** Each agent's contact card now shows its
  configured model and reasoning-effort (e.g. "Claude · Sonnet" plus an effort chip),
  and the agent-detail Supervisor card adds a read-only Skill (role) row alongside the
  existing model / effort / session rows. Display-only, rendered via `textContent`
  (XSS-safe); fields are omitted cleanly when unset. No server/API change — the data was
  already projected into `/api/state` by v0.75.0.

## [0.75.0] - 2026-07-14

### Added

- **Per-agent model and reasoning-effort for wrapped agents.** `supervisor.json`
  per-agent `model` / `reasoning_effort` (resolved per agent, validated against a
  per-CLI effort set), plus `agenttalk wrap --model/--effort` overrides. On the
  `--loop` path the wrapped child argv is injected — Codex `-m <model> -c
  model_reasoning_effort=<effort>`, Claude `--model <model> --effort <level>` — and
  an explicit model/effort already present in the raw launch tail always wins (a
  conflicting config value is skipped with a warning, across every canonical and
  attached/`=` codex flag spelling). Claude reasoning-effort is now supported
  (`--effort {low,medium,high,xhigh,max}`, verified on Claude Code 2.1.207).
- **Restart-safe runtime session fingerprint.** Wrapped session state records a
  fingerprint of the effective `(model, effort)`; when it changes, the next launch
  starts a fresh session instead of resuming a thread built under the old runtime
  config. A first launch — or a pre-0.75.0 session with no fingerprint — adopts the
  current fingerprint silently (no spurious reset), so upgrading does not wipe a live
  conversation.
- **Runtime status in the Team Console.** The agent detail view's Supervisor card
  shows the configured model, reasoning-effort, and session runtime state
  (fresh/resumed + last reset reason), through a projection that never exposes raw
  session/thread ids.

### Notes

- Reasoning-effort validation is a launch-time typo guard against a per-CLI value
  set, not a per-model validator: a value a specific model rejects still fails at
  request time. `--model/--effort` apply only to `--loop` wrapped agents (a warning
  is emitted if passed without `--loop`). Non-wrapped/non-loop agents and the
  headless no-visible-CLI supervised mode remain a later runtime-design slice.

## [0.74.1] - 2026-07-12

### Fixed

- **Resolved dead letters stay resolved in the Team Console.** The dashboard
  Attention endpoint now evaluates the canonical dead-letter records used by
  the CLI, so a supported `agenttalk dead-letter resolve` disposition hides the
  matching row while unresolved dead letters remain visible.

## [0.74.0] - 2026-07-12

### Added

- **Single-agent project initialization.** `agenttalk init --agents claude`
  and `--agents codex` now create valid one-agent rosters that can grow later;
  peer-targeted commands still require an explicit recipient until a peer is
  configured.

### Changed

- **The Team Console keeps project identity visible and root-safe.** Every view now shows the
  selected project's stable display label and absolute root, persists selection by path-derived
  project ID in the dashboard URL and browser title, and scopes dashboard reads and writes to that
  project. Multi-root writes require one explicit full project ID and never resolve display labels;
  explicit unknown, blank, repeated, or ambiguous roots fail closed instead of falling back to the
  primary project, duplicate descriptors for one project ID are rejected at startup, and switching
  projects clears root-bound message, answer, and lead-chat drafts. Project selection now follows
  browser Back/Forward history, and narrow viewports wrap filters and long lead-chat identifiers.
- **Close updates are serialized and generation-bound.** Existing close
  mutations require both the current generation and immutable instance id;
  force-open creates a new instance, and release-barrier publication is bound
  to the published close generation so a failed send or stamp can be resumed
  without sending a second barrier.
- **Lane delivery is recoverable and two-phase.** A non-consumable prepared
  artifact precedes a generation-bound `publish_pending` checkpoint; only a
  committed artifact is valid evidence. Publication and worktree cleanup are
  separately retryable, and no-worktree waivers are advisory records rather
  than release-isolation authority.
- **Runtime persistence is explicitly serialized and recoverable.** Cooperating
  state writers use hardened cross-process locks; append-only JSONL readers
  isolate malformed physical lines; supervisor state uses a validated backup;
  future heartbeats are bounded; and wrapper waiter teardown is generation-bound.
- **Typed review and proposal statuses are strict.** `review-result` accepts
  only `approved|rejected|needs-info`; `proposal-response` accepts only
  `accepted|rejected|countered`. Missing legacy status remains readable but is
  nonterminal, while an invalid present status is rejected or skipped.

### Fixed

- **Windows turn-watchdog termination no longer launches `taskkill.exe`.** Verified
  per-turn process targets are terminated with `os.kill(pid, signal.SIGTERM)`;
  on Windows this is abrupt termination, not graceful shutdown. This eliminates
  the `taskkill.exe` subprocess path that produced the reported popup. The
  production reporter's desktop-heap exhaustion diagnosis is plausible but is
  not an upstream-confirmed root cause. Windows snapshot and start-time helpers
  still launch PowerShell/CIM subprocesses; PID reuse remains possible after
  the recheck, and leaf-first snapshot termination is not an atomic tree kill.
  Those residuals are follow-up hardening, not blockers for this narrow fix.
- **An old wrapper can no longer erase a replacement waiting marker.** Each
  wrapper loop writes a unique wait token and clears the marker only while that
  token still matches.

## [0.73.1] - 2026-07-10

### Added

- **Team bootstrap preflight.** Added `agenttalk supervise --bootstrap-check`, a read-only JSON
  check for roster readiness: operator-facing liaison, supervisor-managed agents, wrapped
  Claude/Codex launch invariants, explicit wrapped `--root`, placeholder launch config, and fresh
  heartbeats.

### Changed

- **Review handoffs now surface untracked files.** Agent manuals and bundled Claude/Codex handoff
  and listen skills now require `git status --short` evidence so reviewers do not miss untracked or
  intent-to-add implementation files.

### Fixed

- **Healthy idle waiters no longer look like a soft-deadlock.** `agenttalk status` only emits the
  multi-waiter soft-deadlock warning when at least one live waiter already has unread work.

## [0.73.0] - 2026-07-09

### Added

- **Project onboarding ledger.** Added `agenttalk onboarding create|list|show|state|record`
  as a native, append-only evidence surface for new-project and existing-codebase analysis.
  Runs capture bounded segment, claim, doc/code drift, and unknown records under
  `.agenttalk/onboarding/<run-id>/events.jsonl`; corrupt lines are surfaced as problems
  without hiding valid records.
- **Dashboard Onboarding view and `/api/onboarding`.** The Team Console now exposes selected-root
  onboarding runs, checklist counts, open drift, blocking unknowns, and ledger-health warnings.
  The projection is read-only, pointer-first, bounded, and does not include raw bus message bodies
  or prompt/output text.

## [0.72.3] - 2026-07-09

### Fixed

- **Dead-letter notice escalations no longer linger as phantom current work.** Resolving a
  dead-letter now closes matching pending wrapper notice threads, dashboard thread/current-work
  projections suppress canonical notice twins, and resolved sink rows are excluded from default
  operator counts.
- **Resolved dead-letters can be archived safely.** Added `dead-letter purge --resolved` with
  dry-run/JSON support, and kept purge guarded by the same operator-facing disposition authority.
- **Manual restart requests now cover healthy idle wrapped agents.** Supervisor restart handling
  treats authorized restart markers as actionable for fresh idle agents instead of waiting for a
  stale signal; protected live restarts require an explicit acknowledgement.
- **Duplicate scoped waiters now exit explicitly as superseded.** A newer same-thread
  `wait --to-request` records a tokened supersession event; the older waiter exits 6 with a stderr
  diagnostic, does not consume messages, and does not clear the replacement waiter's marker.
- **Wrapped setup/worktree stalls are visible in health and Attention.** Failed wrapped turns caused
  by a `git worktree` branch collision are classified as deterministic setup blocks, recorded with
  a safe `worktree_branch_already_checked_out` health reason, and shown in dashboard Attention as a
  `STALLED` item with fixed remediation text.

## [0.72.2] - 2026-07-09

### Fixed

- **Dashboard Attention cards now show the escalation question.** Action-enabled escalation cards
  include a bounded `prompt_excerpt` above the answer box so the operator can see what they are
  answering without opening Sessions first.
- **Dashboard counts are labeled by what they count.** The menu/page badge is the human attention
  queue count; overview/filter/status health buckets now say "Health attention" instead of implying
  they are the same queue.

## [0.72.1] - 2026-07-09

### Fixed

- **Source distributions are now scoped to project artifacts.** The `v0.72.0` sdist was built from
  a local working tree and included untracked scratch/cache/operator files such as `.tmp/`,
  `.pytest-cache-local/`, and local handoff notes. Hatchling's sdist target now has an explicit
  include list for source, tests, docs, specs, and project metadata so local-only files cannot ship
  in the source package.
- **Packaging CI now probes for local-file leaks.** The wheel/build workflow creates sdist sentinel
  files before `python -m build` and then opens the generated `.tar.gz` to assert the sentinels are
  absent while the new-user manual PDF and dashboard static assets are present.

## [0.72.0] - 2026-07-09

### Added

- **Dashboard Learning view.** The Team Console now has a read-only Learning panel that shows accepted
  active lessons by default, including what was captured, who published/curated/owns it, evidence and
  anchor pointers, exposure counts, recent "surfaced to prompt" events, and ledger health warnings.
  The UI deliberately labels exposure as surfaced, not applied: it proves the operational chain
  accepted -> matched -> prompt handoff, not model cognition or compliance.
- **`GET /api/learning`.** Added a selected-root-aware, read-only JSON feed for the learning ledger.
  It supports explicit `status`, `scope`, `tag`, and `limit` filters; defaults to accepted active
  lessons; keeps proposed/stale/retired rows out of the default view; joins pointer-only exposure
  telemetry by lesson fingerprint when available; and degrades corrupt knowledge/exposure lines into
  bounded problem rows instead of 500s.
- **New user manual source and PDF.** Added `docs/AGENTTALK-NEW-USER-MANUAL.md` and generated
  `docs/AGENTTALK-NEW-USER-MANUAL.pdf`, and updated the README documentation map to point new
  operators at the concept-first manual first.

### Security

- **Learning anchor metadata is allowlisted.** `/api/learning` no longer recursively returns arbitrary
  `anchor_evidence` objects. It emits only known anchor pointer fields plus primitive ID/hash/ref-style
  evidence keys, drops body/prompt/output/content-shaped keys, and has a regression covering
  `body`, `prompt`, `prompt_block`, `output`, and nested secret-shaped evidence.

### Changed

- Added a `.gitattributes` rule so checked-in PDF manuals are stored as binary artifacts rather than
  line-ending-converted text.

## [0.71.0] - 2026-07-08

### Added

- **Wrapped agents now receive accepted lessons automatically.** `wrap --loop` computes the same
  active, accepted lesson context that `agenttalk sync` would show for a matching work context and
  injects it into each inbound wrapped turn as a bounded **Lessons to check** prompt section. Wrapped
  children are still forbidden from running inbox/cursor commands such as `sync`, `threads`, `drain`,
  `recv`, `wait`, and `ack`; lessons are framed as advisory memory only, never instructions or
  authorization.
- **Lesson exposure telemetry.** When a wrapped turn receives at least one matched lesson, the wrapper
  appends a pointer-only exposure event to `.agenttalk/knowledge/lesson-exposures.jsonl` after prompt
  handoff. The event records agent/message ids, context scope/tags, lesson refs/fingerprints, and a
  prompt-block hash without duplicating lesson bodies. The reader validates the exposure schema and
  skips malformed or wrong-stream lines without affecting the knowledge ledger or wrapper turn.

## [0.70.2] - 2026-07-07

### Fixed

- **Wrapped supervisor restarts now launch parser-visible same-root wrappers even for stale
  `supervisor.json` files.** The generated PowerShell supervisor normalizes legacy wrapped launch
  argv such as `python -m agenttalk wrap --for NAME ...` by inserting global `--root {ROOT}` before
  `wrap` at launch time. Surviving old wrappers are then visible to the process parser and launch
  barrier as same-root same-agent survivors, preventing manual restart from stacking duplicate
  wrappers for one mailbox.

## [0.70.1] - 2026-07-07

### Added

- **Operator user manual:** Adds `docs/USER-MANUAL.md` as the operator-facing guide for install,
  roster setup, first message flows, dashboard and supervisor operation, lanes, gates, knowledge,
  troubleshooting, and glossary usage. The README now has a documentation map that points operators
  to the manual first and keeps maintainer-oriented architecture and assurance docs linked, not
  duplicated.
- **Executable manual examples:** The manual's first bus workflow consumes reads with `drain`, roster
  examples add agents before assigning roles/groups, and the lane/knowledge walkthrough creates the
  `docs` domain before using it.

## [0.70.0] - 2026-07-07

### Added

- **Capture-learning: a curated "lesson" ledger that surfaces process/craft lessons before agents
  repeat known mistakes.** A new `lesson` note type in the knowledge layer captures reusable
  process/craft learning (review gotchas, CI habits, command hygiene, …) with a required scope,
  trigger, evidence, owner, and review/expiry dates. Capture is open (`agenttalk knowledge publish
  --type lesson …`) but **inert until curated** — only an *accepted* lesson (verified by the
  operator-facing liaison / lead, or a real domain's curators) feeds anything. Accepted, non-expired
  lessons are injected — capped and context-matched — into `agenttalk sync` as a "Lessons to check"
  section (and `knowledge onboard --include-lessons`), so a hard-won habit reaches the next agent
  instead of living in one lead's memory. Lessons age by review/expiry (not code-anchor), can be
  superseded/retracted, and the reader is fail-safe (a malformed lesson never breaks `sync` or hides a
  valid one). Advisory, not blocking — the ledger is the capture+review layer; the best lessons should
  still graduate into skills/tests/gates.

## [0.69.6] - 2026-07-07

### Fixed

- **An interactive operator-facing lead no longer shows "heartbeat stale / unavailable" while it's
  actively working.** A human-launched Claude window (the liaison) usually doesn't export
  `AGENTTALK_SELF`, so the tool-boundary heartbeat hook couldn't tell which agent to stamp and the
  lead only stayed live while explicitly sitting in a wait loop. Now `agenttalk heartbeat --hook`
  accepts a hook-only `--fallback-for <agent>` (resolution order: `--for` → `AGENTTALK_SELF` →
  `--fallback-for` → silent no-op), and `agenttalk supervise --install-activity-hook --interactive-for
  <lead>` installs it for the operator-facing liaison. A supervised worker (which sets `AGENTTALK_SELF`)
  still stamps *itself*, so the shared project hook is safe. Liveness stays honest and fail-closed — a
  missing or stale heartbeat still reads *unavailable*; this only gives a live lead a real heartbeat.

### Added

- **`doctor` now advises on the interactive-lead heartbeat hook** — it classifies the project
  `.claude/settings.json` PostToolUse heartbeat state (none / neutral / fallback / wrong-identity /
  unreadable) and, only when the liaison is actually stale, suggests the fix. Advisory only, never a
  gating error; suppressed when the lead is fresh, wrapped, or a managed lead-loop.

_Fixes Bug 6 — the last item from the 2026-07-06 wrapped-fleet incident report._

## [0.69.5] - 2026-07-06

### Added

- **The dashboard render smoke test now covers every view.** The Node-VM render check introduced in
  0.69.4 (which executed one view) is generalized to drive all six dashboard views through
  `renderActiveView` — overview, flow, attention, lead chat, sessions, and agent detail — asserting
  each renders a non-empty pane against representative payloads. Because `console.js`/`console.css` is
  one shared file that drives every view, a change to it is now smoke-tested across all views in every
  gate and CI run, not just the view that was edited. Skips cleanly when Node is unavailable.

### Changed

- **QA guidance (`qa-strategy`, `tester-qa` skills):** a shared-frontend / dashboard change now
  explicitly requires smoking ALL dashboard views, not only the one changed.

## [0.69.4] - 2026-07-06

### Fixed

- **The dashboard agent-detail view no longer renders blank when you click an agent.**
  `renderAgentDetail` referenced an undefined variable when deciding whether to show the "Restart with
  context" action, which threw at runtime and aborted the render (leaving a blank pane) for *every*
  agent. It now reads the already-resolved state (`info.key`), so the profile renders correctly and the
  Restart action still appears only for a stuck-suspected agent. (The bug was invisible to `node
  --check` and the Python suite because neither executes the browser render.)

### Added

- **A Node-VM render smoke test for the dashboard** (`tests/test_web.py`) that executes the real
  `renderAgentDetail` and asserts a non-empty profile for normal, unwrapped, and stuck agents — a
  committed regression guard for the class of runtime render bug above. It skips cleanly when Node is
  unavailable, so it never reds a Node-less CI runner.

## [0.69.3] - 2026-07-06

### Added

- **The dashboard Lead chat now shows an avatar beside each message** — the operator's avatar on the
  right of their own messages and the lead's avatar on the left of the lead's — reusing the existing
  allowlisted, path-safe avatar rendering used by the roster. Purely presentational and frontend-only:
  message bodies stay text-only (no HTML), avatar images stay allowlisted (never built from a bus
  string), no new network calls, and the styling is scoped to the lead-chat transcript so the shared
  message-row rendering used elsewhere is untouched. Falls back to the operator glyph or a status dot
  when no avatar image is set.

## [0.69.2] - 2026-07-06

### Fixed

- **Wrapper infra-vs-local failure classification is now structured, not substring-guessed.** A failed
  drive is labelled a global-infra outage — which retries under backoff instead of dead-lettering —
  only when a *structured* signal says so: a retryable rate-limit event, or a structured API status of
  429 / 529 / 5xx / auth outage. Legacy free-text markers (`timeout`, `unavailable`, `temporarily`, …)
  are now an ambiguous fallback used only when no structured fact exists, so a local error whose
  message merely contains an infra-like word is no longer misattributed to a provider outage.
  `config_blocked` (preflight / local runtime blocks) keeps first precedence.

### Added

- **Dead-letter records now preserve a bounded, redacted tail of the child process's output.** When a
  turn fails, a size-capped tail of the child `stdout`/`stderr` is persisted with the dead-letter
  sidecar and surfaced by `agenttalk dead-letter show`, so an operator can see *why* a message was
  quarantined instead of only its failure label. The tail is **not** used for classification authority.
  Secrets are redacted before persistence — `Authorization` bearer values and quoted/unquoted
  assignment-style credentials (tokens, passwords) are stripped — and the tail is byte-bounded by
  per-character UTF-8 cost, so a multi-byte (non-BMP) line cannot overflow the cap or split a
  character.

_Context: fixes Bug 4 and Bug 5 from the 2026-07-06 wrapped-fleet incident report._

## [0.69.1] - 2026-07-06

### Fixed

- **Supervisor no longer stacks duplicate wrappers when a crashed one is not fully reaped.** Before
  spawning a replacement, the supervisor re-snapshots and applies a one-live-wrapper-per-agent
  launch barrier: if a same-project, same-`--for`-agent wrapper (or its wait loop) still survives —
  or if the process snapshot is unavailable and a prior launcher may still be alive — it skips the
  relaunch for that poll, enters backoff, and emits a (deduped) decision event instead of piling on
  more wrappers (the duplicate-pileup / self-inflicted rate-limit failure mode). A barrier-held poll
  no longer fakes a launch or consumes a pending manual-restart request.
- **`doctor` warns about a stale generated `supervisor.ps1`** that predates the per-project
  singleton lock (missing `--claim-instance` / `--release-instance`); regenerate with
  `supervise --init --force`. Advisory only.

_Context: fixes Bug 2 and the Bug 3 residual from the 2026-07-06 wrapped-fleet incident report. That
report's P0 (a wrapped Claude agent looping forever on a dead `--resume` session) and the Bug 3
singleton lock for newly-generated scripts were already fixed in 0.69.0 — upgrading resolves those._

## [0.69.0] - 2026-07-06

### Fixed

- **The dashboard roster no longer shows an actively-running interactive agent as "Unknown."** An
  unwrapped agent (one running interactively rather than under the supervisor wrapper — most often
  the human-facing lead) has no wrapper health file, so it previously rendered as *Unknown* even
  while it was clearly alive. The roster, agent cards, agent detail, supervisor rows, avatars,
  counts, and filters now show such an agent as **Active** when it has a fresh heartbeat
  (`last_seen` within 120s — the same freshness window as chat liveness). A missing, stale, or
  invalid heartbeat still renders *Unknown* (fail-closed — an agent that stopped listening is
  honestly not shown as live), and wrapped/managed agents keep their exact health-state rendering
  (Working / Idle · waiting / etc.). Timeline segments and health legends are unchanged (they carry
  historical states with no heartbeat context).

This is a pure client-side presentation correction: `/api/state` is byte-identical (no new server
field), and the read-only dashboard remains byte-identical with actions off.

## [0.68.1] - 2026-07-06

### Fixed

- **Lead-chat is now usable with a normal interactive lead.** As shipped in 0.68.0 the dashboard
  chat treated a live-but-unwrapped operator-facing/sole lead as unavailable and could not resolve
  the operator identity on stores created before 0.68.0 — so the feature was unusable for the common
  single-operator setup. Now: an unwrapped operator-facing/sole lead with a **fresh heartbeat**
  reports available (`unwrapped_live`), while a stale or missing heartbeat still correctly reports
  *away*/*unavailable* (an interactive lead must be actively listening — which keeps its heartbeat
  fresh — to be reachable); and `operator_identity` is inferred as the reserved `operator` principal
  on load for upgraded single-operator stores (operator-facing **or** sole-lead shape), so no manual
  `config.json` edit is needed. The lead-chat transcript no longer rebuilds on every 2s poll (a
  payload fingerprint guards the rebuild and ignores volatile age fields), removing the perceived
  "page keeps refreshing" flicker. The console banner now reports its real version.

### Security

- The `operator_identity` load-time inference does **not** weaken send authority: it resolves only
  to the reserved `operator` principal, only when a lead-chat lead resolves and `operator` is not a
  roster agent, never overwrites an explicit value, and is in-memory only. The authenticated
  `/api/lead-chat` send path and the zero-fallback resolver are unchanged, the liveness change never
  reports available for a lead with a missing/stale heartbeat, and messages queue durably.

## [0.68.0] - 2026-07-06

### Added

- **Lead-chat: talk to the lead directly from the dashboard.** A dedicated 1:1 operator↔lead chat
  in the browser — you type to the lead and its replies render back in the window — built on a
  purpose-built `lead_chat_send` path rather than the multi-recipient send. The lead's pending
  decisions/escalations surface inline as answerable prompts (reusing the existing escalate flow),
  and the chat header shows lead liveness (live / idle / away / unavailable) so you know whether a
  message is seen now or the lead is unreachable. Opt-in and additive; nothing changes for existing
  flows.

### Security

- **Operator becomes a bus sender only through the authenticated web request.** This is the first
  time the human operator sends on the bus, so the authority is scoped tightly: `Store.operator_identity()`
  resolves with **zero fallback** (never derives from the lead — no self-send), the operator send
  happens **directly in the authenticated `/api/lead-chat` request** (loopback + CSRF + session)
  and is **refused from the agent-writable intent queue** (both `lead_chat_send` and the
  operator-answer path fail closed there), and the operator principal is excluded from every
  agents-only walk. A malformed decision-answer returns a bounded `400` instead of erroring.
  **Honest ceiling:** this makes operator-impersonation unreachable via the public API, but on a
  same-machine cooperative bus it is not a cryptographic boundary against a fully-privileged local
  process — identity remains an auditable assertion.

## [0.67.0] - 2026-07-05

### Added

- **Mechanically-guaranteed isolated worktree per lane (`lane assign` provisions by default).**
  A lane assignment now provisions a dedicated git worktree on a fresh `lane/<id>` branch off the
  frozen base SHA (default-on; opt out only with an explicit, audited `--no-worktree` + reason).
  The worker is handed a ready path via the read-only `lane workspace --id <id>` and never creates
  or picks a checkout itself — closing the class of bug where two agents shared one worktree. New:
  `lane workspace`, `lane abandon`, `lane gc` (dry-run first; destructive deletes require an
  explicit flag and never remove an unmerged/only-ref branch — deletion is gated on
  `git merge-base --is-ancestor`, never `--merged`/content-equality).
- **Release-time worktree-provenance backstop.** `lane deliver`/`close` verify the delivered commit
  came from the registered lane worktree (canonical path + common-git-dir + branch/detached-at-tip
  + clean tracked tree, all via live git) and **HOLD (`HOLD_WORKTREE_MISMATCH`)** otherwise; a
  release-class lane with no worktree and no waiver also HOLDs. `close` validates the delivery
  artifact's host-computed provenance (integrity token + branch-tip recompute) and never trusts a
  hand-dropped artifact's self-reported fields.

### Security / Internal

- **First mutating git call, hardened.** A new `_git_write` helper (separate from the read-only
  `_git`) runs an allowlisted set of worktree/branch operations with `GIT_TERMINAL_PROMPT=0`,
  `GIT_OPTIONAL_LOCKS=0`, `-c core.editor=false`, argv-only (no shell), a `--` separator + full-SHA
  base validation (no option injection), a bounded timeout, and loud fail-closed handling. See
  DESIGN.md for the ADR. **Honest ceiling:** the delivery integrity token is a store-local HMAC —
  it defeats a hand-authored artifact but is not a cryptographic authority boundary against a
  fully-privileged local process; on a same-machine cooperative bus, identity remains an auditable
  assertion (Git/OS is the real boundary).

## [0.66.0] - 2026-07-05

### Added

- **60 shaped avatars (agents pick a non-circular look).** Six new avatar families — hexagon,
  oval-muted, oval-vivid, rounded-square, star, triangle (10 each) — selectable per principal via
  the existing `agenttalk avatar set <shape>-<name> --from <self>` (e.g. `star-reviewer`,
  `triangle-security`, `hexagon-architect`). These are **opt-in variety** (`source=chosen` only);
  role-based defaults and the operator avatar are unchanged. `agenttalk avatar list` now groups by
  family.
- **Shape-preserving rendering.** The dashboard now renders a chosen shaped avatar as its natural
  transparent shape (`object-fit: contain`, no circular crop) instead of forcing a circle — so the
  star points, triangle corners, and characters that overhang the badge aren't clipped. This is a
  **data-driven, scoped** change (a validated `shape` flag on the avatar record): the original
  round avatars and the operator badge keep their existing circular rendering untouched. Static
  serving stays exact-key/allowlist-only (the 60 assets are flat `<shape>-<name>.png` filenames —
  no path traversal), and any unknown/removed shaped id degrades gracefully to the status dot.

## [0.65.1] - 2026-07-05

### Fixed

- **Dashboard: stop turning benign client disconnects into console tracebacks.** When a browser
  aborted a connection mid-response (tab close, refresh, a cancelled poll), the server tried to
  write a `500` page onto the already-dead socket and re-raised, so `socketserver` dumped a full
  `ConnectionAbortedError`/`WinError 10053` traceback to the console. The handler now classifies
  client-disconnect exceptions (`ConnectionAbortedError`/`ConnectionResetError`/`BrokenPipeError`/
  `socket.timeout` + the matching POSIX errnos and Windows `10053`/`10054`) and abandons the
  response quietly instead of attempting a doomed error write. A **genuine** server error on a live
  socket still logs and returns `500` unchanged — the classifier is strictly type/errno-scoped and
  never matches a generic exception, so no real error is masked. Covered by `handle_one_request`
  as a lifecycle chokepoint (GET/HEAD/POST/body-read).

## [0.65.0] - 2026-07-05

### Added

- **Agent self-selected avatars + an operator avatar.** Avatars are now a durable per-principal
  preference (`avatars: {<principal>: <avatar_id>}` in `config.json`) resolved through a new
  code-owned allowlist module (`agenttalk.avatars`) instead of being derived only from role.
  New CLI: `agenttalk avatar list [--json]`, `avatar set <id> --from <self>` (self-only — cannot
  target another agent), `avatar clear --from <self>`, `avatar set-operator <id>`,
  `avatar clear-operator`. The human-facing **operator** principal now gets a real avatar
  (ships `operator.png`) instead of always rendering a bare status dot, and this generalizes to
  any roster principal without a role.
- **Dashboard operator descriptor.** `/api/state` gains an additive per-agent `avatar`
  record (`{id, file, source}`) and a top-level `operator` descriptor; unset agents keep their
  exact previous role-based avatar behavior (strictly additive — no field removed or renamed).
  Static avatar serving stays allowlist-only (no config value is ever joined into a served path),
  and any bad/missing/invalid preference degrades gracefully (chosen → operator-default →
  role-default → status dot) — it never bricks `load_config`, `/api/state`, or the render.

### Fixed / Hardened

- **Reserved the `operator` principal name.** `operator` (and every reserved principal) is now
  rejected as an assignable agent name at `init` / roster add / roster rename, so an agent can no
  longer collide with the reserved human-operator avatar key. (Caught by the adversarial review of
  this release.)

## [0.64.1] - 2026-07-05

### Fixed

- **Security CI (gitleaks) false-positive on redaction-test fixtures.** v0.64.0's
  supervisor-observability tests plant synthetic secrets to prove the redaction path strips
  them; one high-entropy fixture (`sk-ABC123SECRET`) tripped the `gitleaks` secret-scan job,
  reddening the `security` workflow (the code and redaction behavior were correct — the scanner
  flagged the *bait*). Fixed without weakening detection:
  - Added a scoped `.gitleaks.toml` (`[extend] useDefault = true`, so **all** default rules stay
    active) with a single **marker-only** allowlist regex `sk-FAKE-AGENTTALK-[A-Za-z0-9._:-]+` — a
    reserved prefix no real key uses. Redaction/security tests must use this marker for synthetic
    secrets; a real `sk-<random>` key anywhere still trips (verified: a non-marker high-entropy
    `sk-` is detected, the marker form is allowlisted).
  - Renamed the existing redaction fixtures to the reserved marker form (redaction assertions
    unchanged — they still verify the path/token is stripped).
  - Added `.gitleaksignore` with **only three exact** `commit:path:rule:line` fingerprints for the
    historical high-entropy findings the marker regex can't retroactively match, so full
    git-history scans pass without any path-broad allowlist. No default rule is disabled.

## [0.64.0] - 2026-07-05

### Added

- **Supervisor observability (`agenttalk supervisor` read + decision-event ring).** See the
  supervisor's per-agent assessment and its actual `plan_actions` decision **verbatim** (action +
  state + reason, every branch distinct) via `agenttalk supervisor` (+ `--json`) and a compact
  line in `agenttalk status` — built from the same planner call the supervisor runs, so the view
  cannot drift. Read-only + fail-safe (missing/torn state degrades to a warning + exit 0).
- **Bounded, redacted decision-event ring** at `.agenttalk/state/supervisor-events.jsonl`
  (outside the message store): per-agent transitions + periodic poll summaries, **count-capped
  (512) + tail-capped (256KB)** (never unbounded), **token-only** (no config-blocked/restart free
  text). Treated as untrusted on read — persisted rows are sanitized to the known token-only
  schema before display/dedupe (non-finite epochs rejected, unknown keys dropped, secret-like
  text never echoed), and `supervisor --json` embeds only redacted report/plan projections. All
  lock/read/write/torn failures are swallowed, so the ring can never block or alter the loop.
- **Accurate lead liveness.** The human-facing (operator-facing, unwrapped) lead now reads
  active/idle from recent bus activity (`last_seen`) instead of "unknown" when it has no wrapper
  health snapshot — scoped to the genuinely health-missing lead (a wrapped TTL-stale lead is not
  forced active), display-only.

## [0.63.0] - 2026-07-05

### Fixed / Added

- **Supervisor & wrapper reliability (Slice 1).** Ends the failure class where a struggling agent
  spam-loops and floods the escalation channel:
  - **B4 broken-session resume give-up** — after K=2 consecutive *session-attributable* failed
    resumes of the same session, the wrapper starts a fresh session instead of respawning a broken
    one forever (with one deduped continuity-loss notice). `known_global_infra`, supervisor/operator
    kill, crash-mid-turn, and `config_blocked` failures don't count — on both the message-drive and
    the lead-loop cadence paths (shared classifier).
  - **B1 dead-letter escalation dedup** — one pending operator escalation per (agent, message-id)
    via the existing attention/disposition layer; release is disposition-only (an ack/reply without
    a resolve keeps the latch, so a stuck message can't flood ~150 near-duplicate escalations); a
    worsening transition still resurfaces a fresh notice; a torn notice log fails open to exactly one.
  - **B2 finite, duration-aware infra ceiling** — a persistently-failing infra-classed message
    escalates once and quarantines (bytes preserved, requeue-able) after an elapsed floor + minimum
    attempts, never a silent infinite loop; `0`/negative config falls back to the default.
  - **B3 recovery-authority hardening** — heartbeat freshness is the liveness authority (a fresh
    `working_silent` with a stale heartbeat no longer defeats auto-recovery); `request-restart`
    carries an authorization envelope re-validated at plan time (a stale authorization refuses
    loudly); a protected agent with a fresh heartbeat isn't force-killed without a second live-kill
    ack; the restart marker clears only after the first fresh heartbeat; a per-agent relaunch
    cooldown bounds restart-hammering.

## [0.62.1] - 2026-07-05

### Fixed

- **Wrapper: stop the Windows pipe-teardown finalizer spam.** `_ProcStream.__init__` is now
  exception-safe after `Popen`: if a post-spawn step raises (e.g. `stdin.write` to a child that
  exited immediately), it stops the watchdog/work-heartbeat workers, closes stdin/stdout through
  the benign-pipe-teardown suppressor, terminates and bounded-waits the child, and re-raises the
  original error. This prevents the child's stdout `TextIOWrapper` from being left to GC
  finalization, which on Windows raised a repeated `OSError: [Errno 22] Invalid argument`
  ("Exception ignored while finalizing file …") in the agent's window on every failing turn.
  `make_drive` keeps its existing spawn/exec infra classification (retryable, not poison).

## [0.62.0] - 2026-07-05

### Added

- **`assurance-scan` devkit skill + `python -m agenttalk.assurance` runner (stdlib-only).**
  A codebase-adaptive *evidence producer*: it detects the stack(s) by marker files, runs
  only applicable + installed tools, and emits one normalized typed JSON artifact plus a
  human summary. It never decides GO/HOLD (the gate/close consumes the artifact) and never
  mutates the repo.
- **Fail-safe gate, fail-honest per-dimension attestation.** Missing optional tools are
  skipped (never bricks a local run); a skipped/errored *required* security/deps/secrets
  scan forces SECURE to UNKNOWN (never silently "good"); GOOD/ROBUST/SECURE each require
  executed evidence for their dimensions.
- **Delta-vs-baseline** blocking (new/worsened block; unchanged/accepted don't;
  accepted-expired is gate-visible), **accepted-findings** requiring
  fingerprint+reason+owner+scope+expiry (blanket scopes rejected, expiry fail-closed), and
  a **self-waiver guard** that surfaces a manifest/baseline change in the scanned range
  (including a new untracked baseline) as a distinct blocking evidence line.
- **Provenance in every artifact** (scanned SHA, dirty flag, changed files, manifest+baseline
  hashes, resolved package path, tool versions) with a package-outside-repo HIGH finding, and
  **universal hygiene checks** (NUL/control bytes, BOM, mixed-EOL, `git diff --check`, and a
  declared-but-unexecuted generated-artifact check).

### Changed

- assurance-scan now fails closed on unknown top-level manifest keys and unknown
  profile keys. Annotation data must live under an allowed manifest block or an
  explicit future schema field, and scan scope must use top-level `paths`
  instead of ignored profile-local include/exclude keys.
- assurance-scan malformed accepted-finding expiry values now produce a blocking
  manifest validation finding and artifact instead of a raw traceback.
- assurance-scan attestation letters now require executed evidence for each
  relevant dimension, and baseline findings are marked fixed only after their
  originating tool actually ran.

## [0.61.0] - 2026-07-05

### Added

- **Team Console: colored "who's talking to whom" lines.** The Conversations flow
  graph colors each edge by its sender (deterministic, stable per-agent color),
  adds directional arrowheads, an edge tooltip, and a participant legend.
  Active-review edges keep their animated dash on top of the sender color.
- **Per-agent avatars.** Ten role-themed avatars render on agent cards, the
  agent-detail header, relationship-graph nodes, and the lead card, each with a
  live status-dot badge. Mapping is role-normalized + provider-scoped; an unmapped
  agent or a missing/broken image falls back to the plain status dot. Served from a
  fixed startup allowlist (no path traversal).
- **Full conversation history + an Archived section on the Sessions page.** Sessions
  now splits into Active (open threads) and Archived (this session's
  closed/superseded conversations). Archived is lazy — it fetches only when opened —
  via a new read-only, paginated `GET /api/threads?state=closed` endpoint
  (newest-first, cursor pagination, hard-capped, envelope-only stubs with no message
  bodies). Clicking any thread loads its full transcript via the existing per-thread
  endpoint; the 2-second `/api/state` poll stays light (closed count only).

### Changed

- Thread derivation is refactored into one shared classifier producing both the
  active `/api/state` rows and the terminal history rows, so the two cannot drift.
  `/api/state`'s output is unchanged (parity-tested) and the new endpoint is
  fail-safe — an endpoint error can never affect `/api/state`. All additive and
  read-only; the actions-off console and `/api/state` stay byte-identical.

## [0.60.2] - 2026-07-04

### Fixed

- **Team Console compose/inbox forms no longer reset every 2 seconds.** The
  console's 2-second `/api/state` poll rebuilt the whole view, wiping in-progress
  dropdown selections, typed text, and focus in the action composer (and the inbox
  answer composer). The poll now skips re-rendering while an action field is
  focused, and composer field values persist across any rebuild. Actions-disabled
  (read-only) behavior is unchanged.
- **Flow-graph line thickness is capped.** Edge width scaled linearly by raw
  message count with no cap, so a heavily-used pair on a busy bus rendered as a
  solid blob. Thickness is now a gentle capped log scale (~1px floor, ~6px cap) so
  edges read as lines regardless of volume. Frontend-only (`web_static/console.js`).

## [0.60.1] - 2026-07-04

### Fixed

- **Cross-path operator-answer double-send (the v0.60.0 known-limitation).** Both
  the browser intent drain and the CLI `relay operator-answer` now route their
  final send through a single `Store.send_operator_answer_atomic`, which — under
  the non-reentrant `_config_lock` — re-runs the ownership/pending resolver and
  then sends while still holding the lock. The loser of a browser-vs-CLI race
  re-reads fresh state, sees the escalation already answered, and is denied
  (`not_pending`) with zero sends. Fail-closed on lock timeout, unreadable state,
  recipient mismatch, or a rejected send. The browser drain keeps its two-phase
  crash-recovery reconcile ahead of the atomic send. Reviewed by both bus
  reviewers plus a fresh adversarial concurrency pass (8-way race + mixed
  drain/relay race + 30 stress iterations, all single-send).

## [0.60.0] - 2026-07-04

### Added

- **Operator inbox: answer escalations from the browser.** The Team Console
  (with `--enable-actions`) lets the operator-facing agent answer a pending
  escalation another agent raised, without a terminal. A new `answer_escalation`
  intent kind (`{to_request, body}`) flows through the intent-queue write spine;
  the supervised executor remains the sole authority boundary. A shared resolver
  `resolve_operator_answer_target` enforces — as distinct fail-closed predicates —
  that the target is a *pending* `needs_operator` escalation *owed to the resolved
  actor* (never self-answerable, never unowed, never a coalesced duplicate), and
  the answer is relayed with server-injected `operator_answer`/`operator_origin`
  meta (the browser cannot supply identity or control meta). A dedicated two-phase
  drain reconciles any prior delivery before the live check, so a crash-after-send
  retry is marked applied and never double-answers. `/api/attention` gains
  `answerable`/`answer_escalation` annotations only when actions are enabled
  (the actions-off response and `/api/state` stay byte-identical); the console
  renders a plain-language inbox + answer composer behind the actions gate.

### Changed

- **`relay operator-answer` now refuses a coalesced wrapper twin**
  (`superseded_by_canonical`). The CLI relay and the browser answer path share one
  resolver, so answering a redundant wrapper `needs_operator` notice that already
  has a canonical config-blocked / dead-letter attention item is denied — answer
  the canonical item instead. This keeps the browser answerable set equal to the
  displayed attention queue.

### Known limitations

- Answering the *same* escalation via the browser and the CLI relay at the same
  instant can produce a duplicate answer message (a cross-path race on the pending
  check; not an authority/recipient/content bypass). Concurrent browser drains are
  already serialized by the singleton drain-instance lock. Tracked as a v0.60.1
  fast-follow. See docs/ISSUES.md.

## [0.59.3] - 2026-07-04

### Added

- **Team Console shows per-provider capacity.** The supervised `wrap --loop` now
  refreshes each agent's own capacity snapshot inline (failure-isolated, run after
  the idle heartbeat stamp / cursor commit, interval-gated, exceptions swallowed).
  A supervised Codex agent reads only its isolated `CODEX_HOME/sessions` rollouts
  (no operator-home fallback; degrades to `unknown` with `reason=codex_home_missing`
  when unset). `/api/state` gains additive per-provider capacity fields
  (`primary` 5h window, `secondary` weekly window, and context fill), rendered as a
  rich capacity card in the console. All fields are additive — the read-only console
  stays byte-identical when actions are disabled.
- **Real compact-density mode for the console**, driven by CSS variables so spacing,
  font sizes, and card padding collapse consistently rather than per-element.

### Changed

- **Bounded, fail-closed Codex rollout discovery.** `read_codex_rollout` now scans
  newest-first under real budgets (`CODEX_ROLLOUT_SCAN_LIMIT`,
  `CODEX_ROLLOUT_MAX_FILES`) and orders candidate rollout files by each file's own
  `st_mtime` — directory mtimes only prioritize traversal and never order selection.
  If the budget cannot prove the true-newest rollout for the intended thread, the
  read degrades to `unknown` rather than returning a possibly-stale value presented
  as observed; a requested thread id that cannot be proven inside the bounded
  candidate set fails closed instead of falling back to another thread's rollout.
- The console's secondary capacity window drops the redundant weekly-label suffix.

### Fixed

- Four fail-closed hardening fixes on the web control plane and store: the CSRF
  gate rejects a non-ASCII token with `403 bad_csrf` instead of raising; the action
  rate bucket is guarded by a lock; the intents audit ring evicts oldest-first by
  `(mtime, name)` under its byte cap; and `drain_intents` short-circuits when the
  kill switch is engaged.
- Documented the bounded-discovery known-limitation for shared `~/.codex` operator
  homes (a manual `agenttalk capacity` against a shared home may read `unknown`
  rather than best-effort; supervised wrapped Codex stays isolated per-agent).

## [0.59.2] - 2026-07-04

### Fixed

- **Supervisor process cleanup now uses typed process ownership instead of same-name
  process matching.** The planner no longer treats a matching `--for <agent>` or
  same-root CLI row as a kill target by itself. It emits explicit kill targets
  only from confirmed launchers, direct same-root `wrap --for <agent> --loop`
  wrappers, direct same-root waits plus a bounded brain climb, strict live
  parent/child chains, or exact versioned provenance. Every target carries a
  `reason`/`source`, and suppression counters report equal/unparseable/inverted
  starts, foreign roots, same-root other agents, shell boundaries, unknown rooted
  CLIs, PID reuse, dropped legacy entries, prior TTL/field/request mismatches,
  snapshot-unavailable degraded cleanup, and torn provenance reads.
- **Supervisor launcher cleanup now requires a launch nonce marker.** Supported
  `python -m agenttalk ... wrap --loop` and `agenttalk` console-script launchers
  receive a hidden `--supervisor-launch-nonce` global argument, and a recorded
  launcher is confirmed only when the live command line is readable, parses as
  this root/agent wrapper, passes branch checks, and carries the current nonce.
  Generic pid/start collisions, unreadable command lines, unsupported native
  launch argv, missing/mismatched/malformed/duplicate nonce values, and nonce
  placement after the subcommand or child tail fail closed.
- **Wrapped launch cleanup preserves same-tick children without weakening strict
  edges.** The generated PowerShell captures a pre-launch process snapshot, starts
  the agent, captures a post-launch snapshot, and records new direct children as
  `process_ownership_v1` `launch_child_provenance`. Strict descendant traversal
  still requires parseable starts and `child_start > parent_start`; equality only
  succeeds through launch-baseline provenance.
- **Supervisor PID provenance is versioned and exact.** `managed_pids` entries now
  include `attribution_model`, `root_key`, `agent`, explicit `request_id`, `pid`,
  `start`, `source`, capture/fresh epochs, and `seed_descendants`. Snapshot
  unavailable cleanup uses only TTL-valid exact priors and never derives new
  descendants. Legacy unversioned entries are re-derived only when independently
  attributable on the current tick; unverifiable legacy entries are intentionally
  dropped.
- **PowerShell `Stop-Tree` remains a closed-set executor.** It still stops only
  the target list supplied by Python after pid/start checks; it does not rediscover
  descendants with `Get-CimInstance` or expand by raw `ParentProcessId`.

## [0.59.1] - 2026-07-04

### Fixed

- **Team Console write-spine hardening.** The executor now treats frozen active
  intent plans as untrusted input: before any reconciliation or send it
  re-resolves the current actor, recipient semantics, bus kind, content, and
  exact stable-meta shape. Forged or drifted plans deny with
  `plan_revalidation_failed` and zero sends; `GET /api/intents` surfaces the
  terminal code. Broadcast audiences and reply anchors are re-derived at drain
  time, so roster/group/role/anchor drift requires a fresh operator requeue
  rather than signing an unverifiable frozen recipient.
- **Intent drain liveness and corruption hardening.** Intent and supervisor
  reclaim now use a pid-start-aware owner identity check: live/unknown owners
  remain non-stealable unless a start token is confidently different, and
  unreadable start tokens degrade to conservative `None`. Torn/corrupt active
  intent JSON is quarantined under reset-preserved
  `.agenttalk/control-audit/intents-invalid/` before drain, and terminal active
  intents with unparseable timestamps no longer linger forever against the cap.

## [0.59.0] - 2026-07-04

### Added

- **Team Console write actions — opt-in intent queue (`--enable-actions`, OFF by default).**
  The read-only console becomes a control surface: the browser can **send / reply / propose /
  broadcast** through a fail-closed intent queue. The web tier only APPENDS a typed intent
  (`POST /api/intent`, gated in order by a per-run CSRF token + Host allowlist + `Origin==self`
  + kill-switch + body/rate caps + schema); a supervised **executor** (`agenttalk supervise
  --drain-intents`) is the SOLE actor — it derives the author server-side via `resolve_web_actor`
  (operator-facing liaison, else sole lead, else fail-closed), never trusts a browser-supplied
  identity, and applies through `store.send()`. Delivery is idempotent (pre-send attempt-floor
  dedup + confirmed-dead-only reclaim + a `supervisor.instance.lock` singleton), so a stalled or
  reclaimed executor cannot double-send. `GET /api/intents` shows honest queued/applied/denied
  state; `GET /api/preflight` (read-only) surfaces setup status. **When actions are off the
  console is byte-for-byte the shipped read-only surface** (`POST` → 405, no `/api/session`, no
  `state/intents/`).
- **`agenttalk start` bootstrap.** One command to launch the console + supervisor for an
  already-configured team (guarded `--init-if-absent`: refuses without an explicit location and
  `--agents`; never guesses a cwd/roster).
- **`/` now serves the Team Console.** The old message-list dashboard is removed; `/` and
  `/dashboard` both render the console.

## [0.58.5] - 2026-07-04

### Changed

- **Supervisor Windows launches now default to hidden windows.** `supervisor.json`
  accepts `window_style` globally and per agent/profile (`hidden`, `minimized`,
  `normal`; per-agent/profile wins). Invalid values default to `hidden` with a
  supervisor warning. The generated PowerShell passes `-WindowStyle` on every
  managed launch path, and hidden wrapped agents set a wrapper marker so the
  per-turn CLI child is spawned with Windows `CREATE_NO_WINDOW` instead of
  opening its own console.

### Fixed

- **Wrapped Windows agents suppress benign child-stdout pipe teardown errors.**
  Windows overlapped stdout pipes can raise `OSError: [Errno 22] Invalid
  argument` or broken-pipe errors during normal end-of-stream teardown. The
  wrapper now treats those as ordinary stream exhaustion instead of logging
  finalizer noise or classifying a completed turn as an infra/outage failure.

## [0.58.4] - 2026-07-03

### Added

- **Bounded in-turn work heartbeat (wrapped-Claude false-STUCK fix).** During a long
  non-streaming turn the idle heartbeat is blocked and the framework heartbeat stamps only
  on streaming progress, so a wrapped Claude (`stuck_after_seconds=180`) could be falsely
  STUCK_RECOVERed mid-turn during legitimate >180s silent work. A new wrapper-side ticker
  (`agenttalk.wrapper.work_heartbeat`) stamps the same supervisor heartbeat while the
  per-turn child is alive — immediate first stamp, then every `interval_seconds` (default
  30) — bounded by `max_turn_seconds` (default 900): past the cap only real progress
  refreshes liveness, so a genuinely hung silent turn is still recovered at
  `max_turn_seconds + stuck_after_seconds`. Default-ON for wrapped **Claude** continuous
  loop + managed lead-loop; **default-OFF for wrapped Codex** (its 2400s threshold and
  watchdog-preemption math are unchanged) and for one-shot (the ephemeral lifecycle has no
  stale-heartbeat consumer). Config: `work_heartbeat: { enabled, interval_seconds,
  max_turn_seconds, allow_high_interval }` in `supervisor.json` (per-agent block wins over
  global). Guards fail visibly (launch config-blocked path), never silently coerce: an
  enabled config with a non-numeric/non-positive value, or an interval above
  `min(60, stuck_after/3)` without `allow_high_interval=true`, refuses the launch. A failed
  turn still ends with NO fresh heartbeat (the ticker stops, synchronized against in-flight
  stamps, before the failure-path clear). The ticker never writes health and never kills
  anything; a best-effort diagnostics record lands in `state/work-heartbeat/<agent>.json`
  (not a supervisor input).

## [0.58.3] - 2026-07-03

### Fixed

- **Packaging: the wheel is installable again.** v0.58.0–v0.58.2 could not be
  `pip install`ed from a tag — the wheel build failed with *"A second file is being added
  to the wheel archive at the same path: `agenttalk/web_static/console.css`"*. The Team
  Console's static assets live under the packaged `src/agenttalk/` dir (so they already ship
  as package data), and a redundant `force-include` in `pyproject.toml` added them a second
  time. Removed the `force-include`; the assets still ship, verified by an actual wheel build
  + install. (CI ran pytest + bandit but not a wheel build, which is why this slipped through
  three releases — a wheel-build gate is being added to CI.)

## [0.58.2] - 2026-07-03

### Fixed

- **`agenttalk doctor` now surfaces a wrapped supervised-Codex runtime-preflight failure as
  an error.** `_check_supervised_codex` accepted a runtime checker but discarded it, so a
  wrapped supervised Codex that can't run `agenttalk` in its workspace was never flagged — the
  supervised loop could wedge silently. Doctor now runs the runtime preflight for a wrapped
  Codex and reports a blocker as an `error` (details carry the greppable marker
  `agenttalk-runtime-preflight-FAILED`, with the blocker in `fix` and `data[agenttalk_runtime]`),
  so it holds the agent. The preflight itself (`preflight_agenttalk_runtime`) already shipped in
  a prior release; this closes the last wiring gap.

## [0.58.1] - 2026-07-03

### Fixed

- **Supervised wrapped Claude agents now receive write grants automatically.** A wrapped
  agent's supervisor `session_args` is empty, so the `{PERM_MODE}` substitution never
  reached the child — a supervised wrapped Claude launched read-only and auto-denied every
  write (previously worked around by putting `--permission-mode` in the supervisor tail).
  `agenttalk wrap --loop` now applies the same resolved `claude_permission_mode` (default
  `bypassPermissions`) the supervisor uses for a non-wrapped Claude to the child argv. It is
  a no-op for codex, for an empty mode, and when the operator already set `--permission-mode`
  in the tail — in both the separated (`--permission-mode <mode>`) and GNU
  (`--permission-mode=<mode>`) forms, so an explicit operator tail always wins. Retires the
  interim config workaround.

## [0.58.0] - 2026-07-03

The web dashboard is now the **Team Console** — a five-view operator console for observing a
live multi-agent team, recreated from the operator's high-fidelity design. Read-only this
release; operator write-actions (restart / defer / dismiss / requeue) land later behind an
explicit `--enable-actions` opt-in with a CSRF token.

### Added

- **Team Console at `/dashboard`** — five client-switched views: **Team overview** (per-agent
  status / capacity / heartbeat cards + a live activity rail), **Conversations** (a
  who-talks-to-whom graph + active-thread list), **Attention** (a ranked "needs a human"
  queue), **Sessions** (full message-thread transcripts), and **Agent detail** (current work,
  a ~30-minute health timeline, capacity meters, supervisor state, owned domains). Light/dark
  themes, five accents, and a comfortable/compact density toggle, persisted in `localStorage`.
- **`GET /api/attention`** — the ranked attention queue (escalations, gate holds, dead
  letters, lead-loop-unarmed, plus derived stuck agents) as JSON. Envelope-only and
  fail-safe: a corrupt/uninitialized root degrades to `errors`-as-data, never a 500.
- **`GET /api/thread/<rid>`** — one thread's full transcript, the only route that carries
  message bodies. `rid` is validated before any disk touch; messages pass the same
  roster/kind/HMAC validation as the rest of the dashboard; bodies render via `textContent`
  only. Root-aware via `?root=<label>`.
- **`/api/state` per-agent additions** (all absent-not-null): `cli`, `capacity` (rate /
  context %), `wrapped` / `restartable`, `owned_domains`, a synthesized `task` line, and a
  best-effort in-memory `health_timeline`. Per-thread: `verdict`, `active_review`, `opener` /
  `opener_peer`. Schema stays v1 (additive only).

### Changed

- The dashboard's CSS and JS are now **served static assets** (`/static/console.css`,
  `/static/console.js`) rather than an inline string, which lets the console
  Content-Security-Policy drop `style-src 'unsafe-inline'` for `style-src 'self'` — a net
  tightening. The console builds all DOM via `createElement` / `textContent` (never
  `innerHTML`); message bodies and every bus-derived string are treated as untrusted.

### Internal

- `/api/state` remains body-free, now regression-enforced by a body-content sentinel over the
  whole payload (not just key names). The server stays read-only (GET/HEAD only; the
  full-tree-hash no-mutation regression covers the new routes). System font stack (no remote
  fonts) keeps `default-src 'none'` intact.

## [0.57.1] - 2026-07-02

Fast-follow polish for the operator attention queue: the fresh-review nits, the north-star
`--stats` instrumentation, and the F8/F9 review fold (degraded-input warnings now carried on
`--stats`, and an honest no-body-reads claim).

### Added

- **`agenttalk attention --stats`** (add `--json`) — derived counts of what the queue
  routes: surfaced-active total + by source, dispositioned counts
  (deferred/dismissed/resolved/answered-elsewhere), and the oldest active dwell. Same reads
  as the queue (no new state, no writes); it adds no reads beyond the attention-queue
  collector and does not inspect message-body content. The stats view carries the same
  degraded-input warnings as the queue (torn disposition log / no liaison), so a partial read
  never looks complete.
- **`attention show --item`** gains `--include-deferred` / `--include-dismissed` /
  `--include-resolved` / `--all`, so a dispositioned item stays auditable by id (previously
  `show` could only display an active item).

### Changed

- `attention` typed-field validation now requires the **wrapped form** (`meta` with an
  `attention` key); a `meta` without that key is "no typed block" and validates clean, so a
  caller passing a full message `meta` with unrelated keys gets no spurious errors.
- `needed_by` accepts a naive datetime (treated as UTC); the validation message and
  `--needed-by` help now say so (they previously said "timezone-bearing", contradicting the
  parser). Internals now share one ISO parser (`parse_iso_dt`).
- `dead-letter list` and the doctor dead-letter warning now point at the requeue-then-resolve
  flow: a `requeue` re-injects a fresh copy but preserves the original, so a handled
  dead-letter stays listed until you `dead-letter resolve` it. We deliberately do not
  auto-quiet (it could hide a real unhandled poison). README documents the flow.

### Internal

- Removed dead `_now_iso` rank plumbing (never populated) and corrected the `rank_key`
  docstring to describe `sort_items`' actual ordering.

## [0.57.0] - 2026-07-02

**Supervisor hardening**: the external supervisor gains the safety controls that make real,
unattended auto-recovery (kill + relaunch of a stale agent) tolerable to run on a live
machine — an operator kill-switch, an independent dead-man's-switch mail-age alarm, and a
durable OS-level host — plus PID-reuse and restart-reconciliation fixes.

### Added

- **Kill switch** — `.agenttalk/supervisor.kill` (presence-only). While armed, every mutating
  supervisor action is frozen: process kills, launches, state/snapshot writes, and
  notifications all refuse; read-only paths (`--report`, `--plan`, `deadman`, `status`) keep
  working. The guard is re-checked at each mutation boundary (not only at tick start), so a
  switch flipped mid-tick stops the rest of the action. `supervise --report` surfaces
  `kill_switch_active`.
- **`agenttalk deadman`** — an independent mail-age SLO alarm, deliberately separate from the
  supervisor. It is content-blind (envelope only) and fails **closed**: unparseable message
  files or unknown/unparseable actionable ages raise and exit non-zero rather than silently
  passing, so a stalled or corrupted mailbox can never read as healthy. Exit 3 = stale owed
  work or a fail-safe error. Ships with a `deadman.ps1` wrapper for scheduled hosting.
- **Durable hosting** — `supervisor-task.ps1` installs a current-user Scheduled Task that runs
  the supervisor at logon with auto-restart on failure; `docs/supervisor-hosting.md` documents
  the hosting recipe (semantic/content-blind output, no supervisor-state dependency).

### Fixed

- **Start-time-guarded kills** — the planner never emits a kill target without a recorded
  process start time, and `Stop-Tree` skips any target missing a start time, so a recycled
  PID can never be mistaken for a supervised process.
- **Restart reconciliation** — after the supervisor itself restarts, liveness is re-derived
  from the bus heartbeat rather than trusting stale recorded PIDs.

## [0.56.0] - 2026-07-02

The **operator attention queue**: a derived, ranked, deduped read-only view over the
signals that already need a human — pending `needs_operator` escalations, config-blocked
holds, dead letters, gate/close HOLDs, unarmed lead-loops — plus durable operator
*dispositions* (defer / dismiss / answered-elsewhere) so a decision, once made, stays made
until the underlying situation actually changes. Creates no new message kind and no new
work objects; mutates nothing except its own append-only disposition log.

### Added

- **`agenttalk attention`** — a single ranked view of everything awaiting the operator,
  built from cheap state reads (no git/lane recompute; a degraded source becomes a bounded
  warning row rather than blanking the queue). Sources include capacity (threshold-tripped
  only) and published close HOLDs. `--all` / `--include-deferred` / `--include-dismissed` /
  `--include-resolved` widen the view; `--source`, `--limit`, `--json`, and `attention show
  --item` narrow it. The read-only view works even with no liaison/sole-lead configured
  (global sources surface with a `no_liaison` warning; only per-recipient escalations are
  skipped).
- **Operator dispositions** — `attention defer|dismiss|answered-elsewhere --item <id>
  --reason <text>` (defer also needs `--until <ISO>`, validated on write and re-checked on
  read so a bad timestamp never hides a blocking item). Authority is the operator-facing
  liaison, or the sole lead when none is configured, resolved from `--from`/`$AGENTTALK_SELF`
  (no `--by`). Dispositions are **snapshot-bound**: they hide an item only while its
  identifying *content* is unchanged, so a changed source (e.g. a different config fault for
  the same agent, or an expired defer) resurfaces automatically. The legitimacy guard is
  re-enforced on read (the disposition log is untrusted), so a forged/hand-edited line cannot
  hide a blocking item. `dismiss` is refused for blocking sources (`needs_operator`,
  `dead_letter`, and non-advisory holds) — those must be repaired, answered, or deferred,
  never dismissed.
- **Typed escalation fields.** `agenttalk escalate` gains `--decision`, `--why`, `--option`
  (repeatable), `--recommendation`, `--risk-if-ignored`, `--risk-severity`, `--confidence`,
  `--priority`, `--needed-by`, and `--affected`, written as a canonical nested
  `meta.attention` block. Validation is strict at the CLI write boundary (exit 2 on a
  malformed field, nothing sent); the reader is fail-safe (an unparseable block downgrades to
  an untyped item with a warning and never hides the escalation).
- **`agenttalk dead-letter resolve`** — an operator decision distinct from `requeue`: marks a
  poison message handled out-of-band, **preserving** the payload, and drops it from the
  default `dead-letter list`, the doctor dead-letter warning, and the attention queue. The
  central disposition log is authoritative; a best-effort `.resolved.json` sidecar aids
  copied-sink readability. `dead-letter list` gains `--resolved`/`--all`; `dead-letter
  requeue` gains `--force-resolved --reason` to reopen a resolved item (audited).
- **Doctor** surfaces torn/invalid disposition lines (`attention_dispositions`, WARN-only)
  and its dead-letter check is now resolved-aware.

### Notes

- The disposition log (`.agenttalk/attention/dispositions.jsonl`) is append-only,
  latest-valid-by-(item, action-family), fsync'd under the store lock, skip-invalid on read,
  and preserved by `reset` and `reset --archive`.
- Capacity surfaces only when a snapshot is threshold-tripped (a rate limit reached, or
  primary-budget/context ≥ 90%); close HOLDs surface for PUBLISHED closes whose snapshotted
  verdict is HOLD (read cheaply, no gate recompute). Routine headroom and in-progress closes
  stay silent.
- v1 limitations: no bulk/group dispositions (one `--item` at a time); dedupe is
  display-only (duplicate signals collapse in the view but each keeps its own id and can be
  dispositioned independently).

## [0.55.1] - 2026-07-02

Follow-up to 0.55.0: observability for the supervised-Codex launch path, plus a
test de-flake. Advisory only — no supervisor planner/executor/classifier behavior
changes.

### Added

- **Doctor L4 visibility for supervised Codex launches.** A new advisory
  `supervised_codex` check resolves each configured Codex agent's launch (base CLI
  + pinned interpreter) and runs timeout-bounded, exception-safe probes against an
  env that mirrors the *actual* launch — per-agent `agent.env` overlaid on the
  managed pins, with case-insensitive collision detection on the critical launch
  keys (`AGENTTALK_PY`/`CODEX_HOME`/`PYTHONPATH`/`AGENTTALK_ROOT`), matching the
  Windows PowerShell launcher. It reports OK only when the env mirror is genuinely
  full and every required probe passes; a drifted/broken override or a failed probe
  is `WARN` (never a false OK, never a silent `None`). A separate read-only
  `config_blocked_holds` check surfaces parked agents with remediation. Advisory
  (OK/WARN only, no ERROR).

### Fixed

- **Doctor no longer crashes on a corrupt `supervisor.json`.** A valid-JSON but
  non-dict config (a top-level array/string/number) previously raised through
  `doctor.run()` and lost every other check; it now degrades cleanly. Malformed
  per-agent `env` shapes are likewise handled without crashing.
- **Deterministic composing-wait regression test.**
  `test_scoped_wait_composing_extends_when_cursor_exceeds_baseline` now drives a
  fake clock instead of real sleeps, removing a Windows CI flake while still
  failing if the `scan_since = min(floor, baseline)` guard regresses.

## [0.55.0] - 2026-07-02

Wrapped-Codex now works out-of-the-box on Windows. This release closes a whole
class of launch/runtime failures found in the field and a follow-up sweep, across
three coordinated layers. (Subsumes the proactive launch preflight deferred from
0.54.1.)

### Fixed

- **Unspawnable base CLI no longer causes a silent retry-storm (spawn resolution).**
  On Windows there is no `codex.exe` on `PATH` (only an npm shim), so
  `subprocess(['codex', ...])` failed with `WinError 2` / `FileNotFoundError` and
  was misclassified as a transient outage and retried forever. The wrapper now
  resolves the base CLI (`PATH`/relative/absolute + an `AGENTTALK_CODEX` override;
  the npm-Codex shim is followed to the exact vendored native `codex.exe`, else it
  fails closed), and a pre-loop launch preflight blocks before consuming any
  message. Spawn `FileNotFoundError`/`ENOENT`/`ENOEXEC`/`WinError 2/3/193/267` now
  classify as `config_blocked` (launch); `WinError 5`/`EACCES`/`EPERM` as
  exec-denied. A durable, agent+state-validated `config_blocked` hold parks the
  agent (no kill/relaunch) across the health TTL until the operator repairs the
  config; `request-restart` overrides and a clean preflight self-clears.
- **Bus writes work under `-s workspace-write` (interpreter pinning).** `python -m
  agenttalk` assumed `python` was on the sandbox `PATH`, which Codex's
  `workspace-write` tool shell strips. The wrapper now pins an absolute interpreter
  via `AGENTTALK_PY` (plus `AGENTTALK_ROOT`) in the child env, and the prompt and
  the seven Codex bus skills invoke it (with a `python -m` fallback for unwrapped
  use). An out-of-workspace interpreter needs a one-time operator `--add-dir` (the
  sandbox is never auto-widened); `-s danger-full-access` remains a documented
  last resort.
- **A failed required bus write can no longer be masked and committed (bus-write
  classifier).** The Codex adapter now carries `command_execution` exit status; a
  required durable write (`reply`/`send`/`escalate`) that positively fails is
  classified fail-closed and parked/redelivered instead of reported as success and
  losing the reply. Recognition models Python's CLI grammar (interpreter + `-m
  agenttalk` vs script/`-c`/terminating options) and fails open on any unrecognized
  token, so a normal command (e.g. `rg agenttalk reply`) is never false-matched as
  a bus write and a healthy message is never wrongly dead-lettered.

## [0.54.0] - 2026-07-01

### Fixed

- **Wrapped-Codex bus commands no longer denied by the Codex Windows sandbox (field P0).** A wrapped Codex
  managed lead-loop could never complete a turn: the sandbox denied executing the agenttalk console-script
  shim, and Codex tended to reach for an out-of-workspace agenttalk source checkout (a sibling of the
  workspace, outside the sandbox) which was also denied. The denial was misclassified as transient infra and
  retried, so the loop never progressed. The wrapper prompt (`_DEFAULT_RULES` + `_CADENCE_RULES`) and the
  bundled Codex bus skills now anchor all bus writes to `python -m agenttalk` run from the workspace /
  `AGENTTALK_ROOT`, and explicitly forbid cd/import/referencing an out-of-workspace or sibling agenttalk
  source checkout.

### Added

- **`config_blocked` failure class for deterministic exec/permission denials.** Spawn `PermissionError` /
  `EACCES` / `WinError 5`, tight terminal bus-denial text, and `command_execution` bus-denial output now
  classify as `config_blocked` (evaluated before infra/poison). The loop PARKS the head - no cursor commit,
  no dead-letter, no retry storm - escalates once with command/error/remediation, and keeps the heartbeat and
  lead-loop lease fresh until an operator repairs the config and requests a restart. A resume-side denial is
  classified before the fresh-session self-heal, so a bus denial can never be masked by a clean retry and
  committed. Rate-limit (429/529/5xx) stays infra; content-policy stays poison. Wrapper health surfaces the
  parked `config_blocked` state (classified before the generic spawn-error branch) instead of an outage.

### Changed

- **`agenttalk-send` skill no longer suggests an editable source install as an in-turn remedy.** A missing
  `python -m agenttalk` now directs the operator to install agenttalk non-editable into the runtime Python or
  intentionally run from the agenttalk workspace, instead of `pip install -e <path>` (the misconfiguration
  behind the field denial). The skill-currency lint was also extended to catch bare multi-line inline
  `agenttalk` snippets and wrapper cadence prompt text.

## [0.53.1] - 2026-07-01

### Fixed

- **`test-security` skill: Procedure step 4 aligned with the Hard Safety Boundary.** The step-4 wording
  previously allowed live network / destructive probes "unless the operator approved the environment," which
  contradicted the skill's hard boundary. It now states operator approval covers only the in-repo test
  environment and is never a license for external targets, network/DoS activity, exploit development beyond
  the repo test surface, or detection-evasion - consistent with the boundary section.
- **CI: gitleaks false positive on a test fixture.** The redaction test's deliberate fake secret
  (`SECRET_HEALTH_LEAK_74f78b` in `tests/test_wrapper_health.py`, injected and then asserted ABSENT from the
  health output) tripped the `generic-api-key` rule; a targeted inline `# gitleaks:allow` suppresses it
  without weakening the test or broadening secret scanning (no `.gitleaks.toml` / no scanner-wide allowlist).

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.53.1"
```

## [0.53.0] - 2026-06-30

### Added

- **Advisory wrapper agent-health signal (supervisor recovery aid).** The wrapper now classifies an agent's
  recent activity - from the data it already sees (normalized events, the INFRA/POISON/AMBIGUOUS drive-failure
  taxonomy, the turn watchdog, adapter rate-limit signals) - into a small health enum (`idle_waiting`,
  `working_turn`, `working_silent`, `stuck_suspected`, `rate_limited_or_outage`, `degraded_output`,
  `errored_poison`, `errored_ambiguous`, `crashed_or_exited`, `unknown`) and writes it to an adjacent,
  atomically-written `state/<agent>.health.json`. The heartbeat format is untouched and remains the sole
  liveness authority. The supervisor consumes health **advisory-only**: a fresh working state may *delay* an
  automatic recovery, but health is **never the sole reason to kill** - every existing kill prerequisite
  (stale heartbeat, can-confirm-stuck, protected/operator-facing/lead, backoff, readiness cap,
  start-time-guarded targets, wrapped-codex watchdog precedence) still applies, and an explicit human
  `request-restart` is never blocked. Missing/corrupt/stale health (including impossible future-dated
  timestamps beyond a small clock skew) degrades to `unknown` and never vetoes recovery indefinitely. The
  health schema is redacted by construction: no message bodies, prompts, model output, tool output, or
  transcript text. Surfaced as advisory metadata in `status`/`report`/`plan` and the web status. No terminal
  scraping, no spec-kitty coupling.

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.53.0"
```

## [0.52.0] - 2026-06-30

### Added

- **Three new assurance devkit skills - performance, security, and documentation testing.** Fill the
  executable-evidence gaps in the devkit (which already covered code, review, unit/integration QA, and doc
  writing/review):
  - **`test-performance`** - executed benchmark evidence (workload, dataset, budget/baseline, environment,
    warmup, repetitions, variance policy, regression threshold, commands, artifacts, residual risk). Returns
    needs-info rather than a green claim when no stable workload/budget exists; routes optimization to
    `craft-code` after measurement, never optimizes in-skill.
  - **`test-security`** - executable security/abuse-case testing (authz, input validation, injection,
    path/env/command handling, unsafe deserialization, secrets/log hygiene, dependency/supply-chain),
    distinct from `review-code`'s security review. Carries a prominent hard safety boundary: authorized,
    defensive, in-repo testing of this project only - no external targets, no network/DoS, no exploit
    development beyond the repo test surface, no detection-evasion; stop and escalate outside that boundary.
  - **`test-docs`** - executable documentation checks (doctests, snippet runners, link checks,
    generated-reference drift, code-vs-doc assertions), complementing `review-docs` (which stays adversarial
    prose/accuracy review). `routing.md` is corrected accordingly (it previously said not to use a separate
    docs-QA skill).
  All three are `category: assurance`, emit the existing `qa-result` evidence profile (no new profile), and
  install to both the Claude and Codex Agent-Skills dirs. No new generic testing skills were added
  (`test-coverage`/`test-integration`/`qa-strategy`/`tester-qa` already cover unit/integration/automation).

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.52.0"
```

## [0.51.0] - 2026-06-30

### Changed

- **Durable-listening guidance: supervised `wrap --loop` is now the documented default for unattended
  listening.** A chat-window listener is best-effort — the host CLI, a compaction, or the terminal lifecycle
  can interrupt it (Claude Code reaps in-window background `agenttalk wait` tasks, notably across compaction;
  Codex windows tolerate manual listening only as a human-supervised stopgap). The `agenttalk-listen` skills
  (both Claude and Codex copies, in lockstep) now state this, require agents to represent listening as
  best-effort unless running under supervised `wrap --loop`, and forbid claiming always-on listening from a
  chat window — framed via the existing wakes-are-latency-not-state principle (a missed wake costs time, not
  message durability; recovery is via `sync`/`threads` after a restart or compaction). README, the agent
  manual (`docs/AGENT-MANUAL.md`), and the supervisor tutorial are updated to match, and new skill-lint
  content asserts pin the durable-listening contract. Docs / skill / test-content only — no runtime change.

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.51.0"
```

## [0.50.0] - 2026-06-30

### Added

- **spec-kitty lane seam in the `sk-loop` skill — one ordered, validated transition recipe.** Replaces the
  manual two-step seam (`move-task`, then a separate wake) that left tasks half-moved and the loose review note
  that could block a lane move. The skill now: probes the installed spec-kitty CLI + lane set first and fails
  loud rather than emitting a lane the CLI may reject (real 1.0.2 lanes `planned`/`doing`/`for_review`/`done`,
  with `in_progress` only an observed alias for `doing`); re-derives the agent's exact transition from
  `spec-kitty next`; **moves the spec-kitty lane FIRST, then wakes** (never wakes on a failed move; `move-task`
  exit is the sole authority); and routes review feedback onto the agenttalk bus as the durable record, with the
  spec-kitty `--review-feedback-file` written to the OS temp dir **outside** the mission tree so a stray review
  note can never block the move. Approve is `for_review -> done` (the invalid `--to approved` is removed); reject
  is `for_review -> planned` with no default `--force` (operator escape hatch only). Wakes carry a structured
  `transition_key=sk:<mission>:<wp>:<from>:<to>:<verdict>`; a wake missed by a crash self-heals on the ~30s
  sk-loop poll (named as the repair mechanism — do not lengthen the in-loop wait to listen-mode 1800s). A
  cleanliness diagnostic is advisory only and fails open. Skill-only — nothing in the generic core learns
  spec-kitty lanes or flags. New skill-lint guards pin the corrected lanes and forbid the stale forms.
  Docs: `docs/AGENT-MANUAL.md`.

### Fixed

- **`sk-loop` spec-kitty PATH fallback corrected.** The Codex sk-loop sandbox note told agents to fall back to
  `python -m spec_kitty` when `spec-kitty` is not on PATH, but that module does not exist — the console-script
  entry point is `specify_cli:main` (a function), so no `python -m` form resolves. The skill now prefers the
  `spec-kitty` console script (on PATH or its full Scripts/bin path) and documents
  `python -c "from specify_cli import main; main()"` as the only working pure-python fallback; the valid
  `python -m agenttalk` fallback is retained. A lint guard forbids the broken module forms from regressing.

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.50.0"
```

## [0.49.1] - 2026-06-30

### Fixed

- **Test flake: `test_config_lock_breaks_stale_dead_pid` de-flaked at the root.** The test mocked
  `store._process_alive`, but the config-lock stale-break path (`_break_stale_lock`) decides via
  `store._process_liveness` — a *separate* function — so the mock was ineffective. On a CI runner where
  the planted dead-pid (4242) happened to be a live process, the stale-break correctly refused and the 2s
  acquire timed out (the intermittent CI red seen on some OS/Python jobs). The test now mocks
  `_process_liveness -> PROC_DEAD`, making the stale-break deterministic regardless of the runner's live
  pids. Test-only; no production change (the never-break-a-live-holder behavior is unchanged).

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.49.1"
```

## [0.49.0] - 2026-06-30

### Added

- **`test-integration` devkit skill (Tier 1b) — completes Tier 1.** An assurance skill for validating
  behavior across *real* integration boundaries (CLI + store, filesystem, config loading, migrations,
  multiple modules, process/supervisor interactions): prefer real boundaries where deterministic and
  cheap, use isolated temp roots, never fake the boundary under test, avoid unstable sleeps / live
  services, and record the exact command/result. It runs in two modes — emit `qa-result` evidence when
  reporting QA, or `production-handoff` when handing off production test changes. With it landed, all four
  Tier 1 devkit skills (`qa-strategy`, `fix-ci`, `refactor-code`, `test-integration`) are live and the
  devkit routing index is fully de-interimed.

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.49.0"
```

## [0.48.0] - 2026-06-30

### Added

- **`refactor-code` devkit skill (Tier 1).** A production skill for behavior-preserving restructuring:
  state the behavior-preservation scope first, make no behavior change without explicit approval, keep
  changes local and reviewable, and prove preservation with tests (or explain the exact gap). It emits a
  `production-handoff` record and routes behavior changes to `craft-code`, a red baseline to `fix-ci`, and
  broader test work to `test-coverage`. With this, the devkit routing index is fully de-interimed (the
  `Behavior-preserving cleanup` row and `fix-ci`'s step 4 now route directly to `refactor-code`, with no
  remaining "until it exists" fallbacks). Third of the Tier 1 devkit skills.

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.48.0"
```

## [0.47.0] - 2026-06-30

### Fixed

- **Wrapped-codex turn-1 hang (supervised hung-tool recovery).** A supervised `wrap --loop --cli codex`
  agent whose turn wedged on a hung tool subprocess (e.g. a pwsh that spawned a bare `node` REPL waiting
  on stdin) would never complete the turn, never heartbeat again, and became alive-but-permanently-silent
  with no self-recovery. A new per-turn watchdog (`agenttalk.wrapper.turn_watchdog`, default-on for
  continuous wrapped codex) detects this with a TWO-FACTOR signal -- the turn has run past
  `turn_elapsed_seconds` (1800) AND a non-codex tool descendant has been alive past
  `tool_descendant_alive_seconds` (600), which distinguishes a hung tool from long pure reasoning -- then
  kills the per-turn process tree (leaves-first, START-TIME-GUARDED: it re-reads each target's live start
  immediately before the kill and skips a reused PID; fail-open if the snapshot is unavailable) and
  converts the wedge into a recoverable `ambiguous` turn failure (the message stays pending; repeated
  wedges escalate then dead-letter, never poison). It stamps a narrow recovery heartbeat so the supervisor
  does not preempt the in-wrapper recovery. Reported by the orbit launcher team.

### Changed

- **Wrapped-codex `stuck_after_seconds` default raised 900 -> 2400** so the supervisor's stale-recovery
  never preempts the new turn watchdog (which fires at ~`turn_elapsed_seconds` + margin). The supervisor
  now refuses restart-on-stale (warn-only) for a wrapped codex whose `stuck_after_seconds <=
  turn_elapsed_seconds + 300` unless `allow_low_stuck_after` is set; a single shared predicate decides
  whether the watchdog is effectively live so the wrapper and supervisor can never disagree (a sub-floor
  `turn_elapsed_seconds` without `allow_low_turn_elapsed` disables the watchdog AND keeps normal supervisor
  recovery). Known limitation: a legitimately long-running tool (alive past `tool_descendant_alive_seconds`
  within a turn past `turn_elapsed_seconds`) can be killed; raise the thresholds (or set
  `allow_low_turn_elapsed`) for such agents.

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.47.0"
```

## [0.46.0] - 2026-06-30

### Added

- **`fix-ci` devkit skill (Tier 1).** A production skill for making an *already-failing* local or CI
  check green again: read the failing command and its full log first, classify the root cause (code
  defect, test defect, flaky test, environment, dependency, or CI configuration), apply or propose the
  *smallest* fix that addresses that cause, verify, and emit a `production-handoff` record. Explicit
  `Not For` boundaries route pre-failure test planning to `qa-strategy`, broader coverage to
  `test-coverage`, and product-behavior changes to `craft-code`; it never broadens into feature work,
  drive-by cleanup, or self-approval. Routed in the devkit routing index (the `Diagnose + fix a failing
  local/CI check` row is now live, with a precedence rule and a negative trigger against guessing without
  logs). Second of the Tier 1 devkit skills.

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.46.0"
```

## [0.45.0] - 2026-06-30

### Added

- **`qa-strategy` devkit skill (Tier 1).** A planning skill that decides the QA and review coverage a
  change needs *before* tests are written, a diff is reviewed, or a release is gated: it identifies risk
  areas, recommends test levels and review lenses, states which checks are NOT needed and why, attaches
  cost notes (cheap/moderate/expensive), and names the evidence required for close. It emits a
  `planning-artifact` (a plan, never an approval) and routes implementation to `craft-code`,
  test-writing to `test-coverage`, an already-failing CI command to `fix-ci`, and final approval to the
  review lenses. The devkit routing index is updated (the `Decide which tests/lenses a change needs` row
  is now live, with a precedence rule distinguishing plan-only `qa-strategy` from `test-coverage` /
  `test-integration` / `review-code`). First of the Tier 1 devkit skills.

### Fixed

- **Test hardening: `test_web` transient Windows socket flake.** The read-only dashboard HTTP tests now
  route every client request through a bounded-retry helper that retries ONLY transient connection
  errors (`ConnectionError` / `ConnectionAbortedError` / `ConnectionResetError`, including the
  `URLError`-wrapped connect-phase form - i.e. WinError 10053/10054) and re-raises `HTTPError` and any
  non-transient `URLError` immediately, so the status assertions (403/404/405) are unchanged and a real
  failure is never masked. Bounded attempts; on exhaustion the last transient error is re-raised.
  Test-only; no runtime change. Fixes the intermittent `ConnectionAbortedError [WinError 10053]` seen on
  Windows CI.

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.45.0"
```

## [0.44.0] - 2026-06-29

### Changed

- **Skill-currency check: version-stamp lag is advisory, not a hard failure.** A bundled skill whose
  `reviewed-against` stamp lags the package minor is now a WARN-level finding (a re-review reminder),
  not an error. `Finding` carries a `level` ("error" | "warn"), and the source-tree currency test
  fails only on BLOCKING findings. Real drift stays blocking: a missing/malformed stamp,
  frontmatter/category/evidence-profile failure, evidence stub/profile parity drift, a dangling
  continuation, an unreadable file, and a stale/nonexistent CLI token. This fixes the v0.43.0 failure
  mode (a minor bump reddening CI purely via stamp-lag) and removes the chore of re-stamping every
  skill on every minor release; a stamp now changes only when a skill is actually reviewed/edited
  against that CLI minor. `doctor` still surfaces lag as a WARN. (A hard freshness gate, if ever
  wanted, belongs in a release-readiness lens, not generic lint.)

### Added

- **Listen skills: persistent wait-kill guidance.** A new section in `agenttalk.listen` /
  `agenttalk-listen` (Claude + Codex) for when the harness repeatedly kills an agent's background
  `wait`: recognize repeated kills (do not rely on a clean exit code), stop tight-loop re-arming, and
  escalate over the DURABLE bus first (`agenttalk escalate` is a durable send, unaffected by the
  killed wait) to be relaunched under supervised `wrap --loop` - with own-window reporting only as a
  fallback. A killed wait loses real-time push, not queued data (the bus is durable, the cursor
  monotonic); a repeated kill is NOT a `release`/`end`, so the agent stays recoverable rather than
  winding down.

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.44.0"
```

## [0.43.1] - 2026-06-29

### Fixed

- Bundled-skill `reviewed-against` stamps bumped to `0.43` so the skill-currency check passes on the
  0.43 release line. The v0.43.0 release commit bumped the package version but not the skill stamps, so
  the version-stamp ratchet flagged every bundled skill as minor-lagged and the source-tree currency
  test failed in CI. No functional change to the skills, the lint, or the evidence schema.

## [0.43.0] - 2026-06-29

### Added

- **Skill-devkit currency + evidence foundation (Tier 0 of the devkit evolution).** The bundled
  skill set now has a mechanical staleness guard and a single canonical evidence contract, so the
  devkit can grow without silently drifting from the CLI or the assurance gates.
  - **`doctor` skill-currency check.** A deterministic lint validates every bundled skill's
    `agenttalk` / `python -m agenttalk` command and flag tokens against the live argparse surface
    (recursive, including multi-level subcommands like `relay operator-answer`), enforces
    frontmatter well-formedness, and applies a `reviewed-against` version-stamp ratchet that warns
    on a major/minor lag. WARN-only in `doctor` (never bricks the bus); a source-tree CI test fails
    on bundled-source currency regressions. It scans only fenced code blocks + inline-backtick spans
    (never prose), accumulates shell line-continuations, and stops at the `--` wrapper passthrough.
  - **Canonical evidence reference + in-skill stubs.**
    `skills/devkit/_shared/references/evidence.md` defines the typed evidence profiles
    (planning-artifact, production-handoff, review-result, qa-result, close-ack, na-result),
    marking each field BUS-VALIDATED vs SKILL-POLICY-ONLY. Every validator-backed profile is
    gate-tied by a test against the REAL bus validators (`gates.validate_review_result_evidence`,
    `close.apply_ack`), so a profile can never claim a bus guarantee it does not have. In-skill
    stubs are checked for parity against the reference and the skill's frontmatter profile.
  - **Routing index.** `skills/devkit/_shared/references/routing.md`: a task-to-skill table,
    negative triggers, capacity guidance, and the dual-review (context-preserving + fresh-context)
    rules.
  - **`review-code` tightening.** A machine-visible final verdict (APPROVE / APPROVE-WITH-NITS /
    REQUEST-CHANGES, mapped to the bus status; REQUEST-CHANGES defaults `release_blocker=unknown`)
    plus a small finding-type taxonomy.

### Changed

- All bundled skills carry `reviewed-against` frontmatter; devkit capability skills also carry
  `category` + `evidence-profile`. The `agenttalk-lead` / `agenttalk-listen` skills (Claude + Codex)
  are refreshed for the v0.42.0 contracts (managed lead-loop ownership, the `relay` flow) and the
  operator relay now uses `relay operator-answer` instead of the hand-rolled
  `reply --meta operator_answer=true`, which bypassed the relay audit-integrity guard.
- After upgrading, existing users should re-run `agenttalk install-skills --devkit-only --force` to
  refresh installed skills (otherwise `doctor` warns that the bundled vs installed copies differ).

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.43.0"
```

## [0.42.0] - 2026-06-29

### Added

- **Split-identity lead-loop enforcement.** A lead can now run its team-coordination loop as a
  SEPARATELY-SUPERVISED managed identity that OWNS the team mailbox via a renewable lease - closing
  the failure where "the lead silently un-arms its own control loop" (the lead drops out of the loop
  and nothing notices, so the team stalls with no operator-visible signal). Ships as Slice 1 + four
  work packages (WP1-4), each cross-reviewed + gated:
  - **Managed lease + single-consumer guard (Slice 1).** `state/<agent>.lead-loop-lease.json` is the
    ownership truth; a controller acquires/renews it and a verb-guard blocks a second consumer of the
    same mailbox; `lead_unarmed` is surfaced in status/doctor. Lease steal uses a CONFIRMED-dead
    tri-state liveness probe, so a crashed controller is taken over at once while a live or merely
    uncertain one is never displaced (D-12).
  - **Single authority source (WP1).** One `_lead_loop_authority` computes {managed, liveness,
    expired, heartbeat-stale, stealable, armed, guarded}; steal, the armed detector, and the guard
    all derive from it, so they can never drift apart (for a present managed lease,
    `armed == not stealable` in every case). A timing resolver gives the steal path and the
    visibility paths one shared `heartbeat_stale_after`, keeping `store.py` free of supervisor imports.
    A truthy non-dict supervisor config entry coerces to the default via `isinstance` instead of
    crashing status/doctor/supervise/wrap (the corrupt-config coercion class, D-13).
  - **The lead-loop CONTROLLER (WP2): `wrap --loop --lead-loop`.** A long-running supervised process
    that owns the mailbox for its whole lifetime: acquire-before-loop, a combined renew+heartbeat on
    every idle stamp and streaming event, an ownership gate at every cursor-advance boundary (a lost
    lease stops consumption at once), and three exit states the supervisor reads from an exit marker -
    blocked-acquire (HOLD, no relaunch), valid human release/end (deliberate stand-down, no relaunch),
    and crash/lost-lease (relaunch + re-acquire). The lease token is never leaked to the model child.
  - **The proactive CADENCE TICK (WP3).** When the bus is quiet and the cadence interval elapses, the
    controller drives a SYNTHETIC sweep over a bounded, read-only snapshot (ids + summaries, never
    transcripts, never the lease token): it nudges stalled outbound threads and surfaces dead-letter /
    unrouted escalations, spending a model turn only when something is actionable. It is the timeout
    branch of the same loop (no second consumer/thread) and NEVER advances the cursor, records an
    attempt, or enters the dead-letter path; a failed sweep is controller-HEALTH (backoff + an
    escalation retried until it routes), never message poison (D-14). The cadence health view
    evaluates lease-armed state at the SAME resolved heartbeat window the supervisor/guard use (not
    the bare default), so a wrapped controller is never falsely reported down while it still owns the
    lease.
  - **The mechanical liaison RELAY (WP4): `agenttalk relay`.** Carries the operator's words across the
    human<->bus boundary with an audit stamp and NO new message kind: `relay operator-answer
    --to-request <rid>` validates a pending needs_operator escalation addressed to the liaison and
    routes the operator's answer back to the asking lead-loop; `relay operator-command` relays a
    spontaneous operator instruction to a managed lead-loop, fail-closed to the operator-facing liaison
    (audited `--override --reason` aside). Both handlers are authoritative for the reserved audit meta -
    a caller `--meta` can never forge an audit marker or graft routing onto a relayed message (D-15).
- **Operator-facing surface.** `agenttalk managed-lead-loop set|clear|list`, `agenttalk wrap --loop
  --lead-loop`, `agenttalk relay operator-answer`, `agenttalk relay operator-command`. The lead-loop ->
  operator direction stays the existing `agenttalk escalate`. Rationale + decision log: docs/DESIGN.md
  D-12..D-15.

### Changed (behavior)

- Per-agent supervisor config is read fail-closed: a TRUTHY non-dict entry (an operator typo) coerces
  to the default via `isinstance`, never crashing status/doctor/supervise/wrap startup (the
  corrupt-config coercion class, D-13).
- `reset` clears the lead-loop lease, exit marker, and cadence state (all under `state/`) and PRESERVES
  the dead-letter sink.

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.42.0"
```

## [0.41.0] - 2026-06-28

### Added

- **Dead-letter / poison-message handling for the supervised wrapper loop.**
  A message that fails *deterministically* at the head of a mailbox no longer drives
  an unbounded backoff-restart loop (the v0.30.0 known limitation). The continuous
  wrapper loop keeps a durable per-agent attempt ledger
  (`.agenttalk/state/dead-letter-attempts/<agent>.json`, write-ahead before each
  drive so a crash mid-turn still counts), and when a head message exhausts its
  retry budget it is moved to a recoverable, scan-invisible sink
  (`.agenttalk/dead-letter/<agent>/`) and the cursor advances — never advancing
  unless the original bytes are recoverable, never double-delivering. Failures are
  classified three ways:
  - **poison-eligible** (explicit terminal turn failure + crash-mid-turn) →
    auto-dead-letters at a low *consecutive* cap (K=3);
  - **known-global-infra** (spawn/auth/rate-limit/network/5xx, and a recognized
    retryable transport drop — including one that arrives after the handshake) →
    **never** auto-dead-letters; keeps retrying and escalates at a high ceiling (K=20);
  - **ambiguous/unknown** (partial stream, nonzero-after-start, unattributed
    terminal) → escalates and auto-dead-letters only at the high ceiling.

  No failure class can silently loop forever. Restore is an explicit **requeue**
  (a fresh-id message; no cursor rewind, so it cannot immediately re-poison).
- **CLI + visibility.** `agenttalk dead-letter list` / `show` / `requeue`; the
  dead-lettered count is additive in `status`; `doctor` goes loud when a message
  was dead-lettered with no escalation target, when the lead is the only route, or
  when the sink is unreadable.

### Changed (behavior)

- Failure classification is fail-closed and conservative: poison markers are narrow
  and qualified (e.g. token-bounded HTTP 413, not bare substrings); an unattributed
  terminal failure defaults to *ambiguous* (high ceiling, never the low poison cap);
  and an explicit retryable transport drop — even after a turn has started — is
  classified **infra** (retry + escalate, never auto-dead-letter).
- Dead-letter scope is the **supervised wrapper continuous loop only**; manual
  `listen` and one-shot turns are unchanged (documented v1 boundary).

### Fixed

- A `claude` session that fails to resume now self-heals (fresh session) rather than
  wedging the loop.
- Sink writes are collision-safe and recoverable: a pre-existing payload is never
  overwritten; new bytes land in a uniquely-named sibling, recorded with the original
  message id.

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.41.0"
```

## [0.40.1] - 2026-06-28

### Fixed (fast-follow hardening; origin: 2026-06-28 audit clusters C4/C5)

- **`roster --expertise`** derives curated-note authorship from the curated view
  (not the latest event), so a later uncurated publish can no longer drop a
  verified author's credit. Registry roles + lane-delivery history remain primary.
- **Knowledge anchor staleness now fails closed.** A path/symbol note with no
  `verified_against_sha` baseline is hard-stale (`missing_verified_baseline`); a
  pathless `wp` anchor is hard-stale (`unsupported_wp_anchor`); a `request`
  anchor's `msg_id` must match exactly (no fallback to `request_id`); any anchor
  scan/read failure resolves to unresolvable rather than fresh. All excluded by
  default; visible with `--include-stale`.
- **Knowledge writes are durable and single-path.** `publish` and `curate` share
  one append helper (append under lock + `flush` + `fsync`; directory fsync is
  best-effort and Windows-guarded), preserving append-only history and the
  reader's torn/invalid-line tolerance.
- **`knowledge onboard` is bounded** — `--limit` (default 20), grouped by domain
  then type, deterministic order.
- **Lane delivery is verified before the lane clears.** `lane deliver` reads the
  written delivery artifact back and validates its shape *and* that it records a
  GO verdict with no holds; any mismatch leaves the lane active and exits nonzero.
- **Restart-marker race fixed.** `write_restart_request` and
  `clear_restart_request` share the config lock; a stale clear can no longer
  remove a newer restart marker.

### Changed (behavior)

- `knowledge onboard` output is now capped at `--limit` (default 20).

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.40.1"
```

## [0.40.0] - 2026-06-28

### Security / hardening

Post-audit hardening batch — origin: the 2026-06-28 fresh independent audit (six
independent fresh reviewers). Fixes a cluster of fail-open / authority defects the
normal review cadence had shipped. See `docs/audit-2026-06-28.md`.

- **Gates fail-closed (`gates.py`).** `gate set` / `gate waive` refuse to mutate a
  corrupt `gates.json` (no longer silently clear a corruption HOLD), serialize the
  full read-modify-write under a lock, and treat a required gate recorded under a
  mismatched scope as blocking (not absence=pass). A `severity=blocker` gate now
  HOLDs on every status except validated `green` / active `waived` — a `skipped`
  blocker no longer returns GO. New `tests/test_gates.py`.
- **Lane shared-path approval is all-matching authority (decision D-11).** A touched
  shared path is cleared only when **every** matching shared entry has a fresh
  approval recorded against that entry by an authorized approver. The
  previously-dead `HOLD_SHARED_WRONG_APPROVAL` is now emitted; verdicts revalidate
  persisted approvals against the current epoch/registry; validation rejects
  duplicate normalized shared globs. There is no winner-picking between overlapping
  globs (the ordering heuristic was proven unsound).
- **Wrapper one-shot + release authority.** The one-shot reviewer loop uses a scoped
  receive (no starvation behind unrelated traffic) with a bounded timeout, clears
  its `.waiting` marker on every exit, and `is_release_authorized` delegates to the
  single `loop_exit_relay_authorized` resolver.
- **End-to-end regression test.** New `tests/test_e2e_lifecycle.py` drives the real
  CLI over a temp store + git through the full lifecycle, asserting exit codes, JSON
  verdicts, on-disk state, the `reset` durability boundary, and negative assertions
  that the above bugs stay fixed.

### Changed (behavior)

- A `skipped` `severity=blocker` gate no longer reports GO (it HOLDs).
- Deliberately-overlapping shared lane entries each require their own approval.

Both are stricter (fail-closed) than before.

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.40.0"
```

## [0.39.0] - 2026-06-28

### Changed

- **Stand-down authority: loop-exit (`release` / `end`) now requires a typed,
  human-origin authority envelope.** Idle agents always keep listening; only a
  typed `release`/`end` carrying an authority marker from the authorized relay
  stands an agent down. A lead can no longer take an agent offline with casual
  prose — and prose, notes, and sign-offs never exit a loop.
  - New `release` flags: `--relay-human` (relay a human stand-down) XOR
    `--emergency` (narrow lead override for a malfunctioning agent); both require
    `--reason`. A bare or unauthorized `release` exits `2` and sends nothing.
  - Authority metadata: `release_authority=human|emergency`,
    `operator_decision` / `emergency` / `operator_report_required`, and a
    required `authority_reason`.
  - A single shared classifier (`classify_loop_control` →
    `stop` / `invalid_control` / `ordinary`) replaces the wrapper's old
    `is_stop_signal` and is mirrored in the listen + sk-loop skills. An invalid,
    unmarked, or unauthorized `release`/`end` is committed (so it never
    redelivers) and reported, and the loop **keeps listening**.
  - Authorized relay = the `operator_facing` liaison if configured, else the sole
    active `role=lead`; fails closed if neither exists (distinct from the broad
    kill-protection set).
- **Behavior narrowing:** a *received* unmarked `end` no longer winds down peers.
  Previously `kind=end` from any sender stopped a listener; now a received `end`
  must carry the authority envelope. `agenttalk end` still lets the **caller**
  leave and export its own transcript (self-exit is unchanged).

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.39.0"
```

## [0.38.0] - 2026-06-28

### Added

- **`agenttalk knowledge` — a lean append-only pointer layer for durable team
  memory (middle-tier Phase 2).** Typed pointer-notes (`seam` / `gotcha` /
  `decision` / `pointer`) anchored to code or threads — not a knowledge database.
  - `knowledge publish` / `curate (verify|retract)` / `pull` / `search` /
    `onboard`. Store at `.agenttalk/knowledge/notes.jsonl`, append-only, current
    view = latest valid event by `(domain_id, key)`, preserved by `reset` like
    `domains.json`.
  - **Capture-open, curate-gated.** Any active agent may publish an `uncurated`
    note; domain owners/curators (and a lead override with a required reason)
    verify, supersede, or retract. Uncurated notes never shadow verified ones in
    the default `pull`.
  - **Anchor-relative staleness.** A note is hard-stale only when its anchor
    changed between `verified_against_sha` and HEAD (path renamed/deleted/
    changed), or its domain / registry hash / SHA is gone; HEAD merely moving
    with the anchor unchanged is a `verified_sha_not_head` caution that is still
    shown. Fails closed when git cannot determine the anchor delta.
  - **Pointer-not-mirror.** Bodies are byte-capped, untrusted data carrying the
    insight not already in the artifact; consumers reverify anchors before acting.
  - Persisted records are validated **as events** (registry hash, SHA shape,
    allowed authority states, event-kind/state matrix), so a forged `verified`
    publish cannot bypass the curate gate and a malformed line cannot hide a
    valid note.
  - `roster --expertise` derives from domain roles + lane-delivery history
    (curated note counts only as a weak secondary; never raw uncurated counts).
  - `doctor` surfaces corrupt/torn knowledge lines.

### Notes

- **Generic and advisory.** agenttalk owns schema, validation, and staleness; the
  project owns `domains.json`, note keys, and curation decisions. Coexists with
  spec-kitty (knowledge may point at WPs but never becomes WP state).
- Known (fast-follow): a curate event without `curated_at` / `updated_at`
  timestamps is currently accepted. This is audit-completeness only — it does not
  affect the curation gate, folding, or staleness.

### Upgrade

```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.38.0"
```

Additive and opt-in; knowledge is used only when you `knowledge publish`.

## [0.37.0] - 2026-06-27

The lane deliver-gate (middle-tier Phase 1). A **lane** scopes an assignee to a
domain (and optional path subset); `lane deliver` gives a trustworthy
point-in-time verdict that the work is in-bounds, current, gate-clean, and
merge-tree-clean — consuming the existing domains registry and assurance gate.
Advisory by design: it is a coordination gate that produces durable evidence, not
a file lock, a Git authorization, or a parallel gate.

### Added
- **`agenttalk lane` (assign / check / deliver / status` + `approve-shared`).**
  - `assign` opens a lane over a `domain_id` (+ optional repo-relative path
    prefixes), stamping base/target SHAs, epoch, and the domains registry hash;
    disjointness vs other active lanes is validated under a lock (read-validate-write).
  - `check` (read-only) and `deliver` compute changed paths from `git diff
    --name-status -z -M -C`, classify them with the **same** matcher as domain
    ownership (segment-aware, casefold-consistent), run **merge-tree** as the
    conflict authority (honest-degraded → HOLD; never infers clean), check the
    assurance gate, and return a pure GO/HOLD verdict with stable hold codes
    (out_of_bounds_path, unowned_path, domain_overlap_path, shared_path_*,
    active_lane_overlap, stale_epoch/registry, merge_conflict, gate_hold, …).
    Exit 0=GO / 3=HOLD, composing with `gate check` / `close check`.
  - `deliver` on GO writes a **durable delivery artifact** outside the
    reset-cleared lane state *before* clearing the lane (and re-validates the lane
    fingerprint under the lock — fail-closed on a raced reassign); on HOLD the
    lane stays active.
- Lanes **consume** the assurance gate and never mint a green release-blocker
  gate. `agenttalk reset` clears active lanes (with a warning); malformed lane
  state fails closed for lane commands only, never bricking send/wait/status.

### Notes
- Generic and advisory: agenttalk owns schema/verdict/diff-parsing/stale-checks;
  the project owns `domains.json`, shared-path policy, lane ids, assignees, target
  refs, path subsets, and required gates. Coexists with spec-kitty (domains
  constrain WP work; WPs never mint domains; deliver doesn't mutate WP state).

### Upgrade
```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.37.0"
```
Additive and opt-in; lanes are used only when you `lane assign`.

## [0.36.0] - 2026-06-27

Ephemeral adversarial reviewers (evidence-only). The lead can ask the supervisor
to spin up a **fresh, one-shot agent** for an independent adversarial review of a
scope; it files a typed `review-result` that feeds the gate/close, then is reaped
and its temporary identity retired. Fresh-by-construction = no in-context
blindness. **Disabled by default**, capped, and supervisor-gated — and it
produces *evidence only*, never a counted sign-off (so a lead can't manufacture
specialist signers; P3 integrity is preserved).

### Added
- **`request-launch` + a supervisor launch-request lifecycle.** The lead drops a
  data-only `request-launch` marker (whitelisted profile/skill/roles, scope at a
  full SHA, bounded prompt); the supervisor atomically claims it and runs a
  **separate one-shot lifecycle** (claimed → rostered as a unique `adversary-*`
  identity → one bus `review-request` → launched via a one-shot wrapper with a
  fresh session → completed only on a schema-valid `review-result` → reaped +
  retired). **No auto-restart, ever**; a startup janitor idempotently reaps
  orphaned `adversary-*` launches after a crash; process trees are killed on
  timeout/failure.
- **Evidence-only outcome:** an approved ephemeral review is evidence that can
  block or counter a close; it does **not** count toward any required specialist
  sign-off. Rejected → counter/remediation; needs-info/malformed/none → HOLD. The
  supervisor never synthesizes approval from prose.
- **Cost & authority controls (disabled by default):** `ephemeral_reviewers.*`
  config — `enabled`, `max_concurrent`, per-hour/per-day caps, timeouts,
  prompt-size cap, allowlisted profiles/skills/roles/groups, `require_authorized_lead`.
  Authority is stricter than release fallback: operator-facing or sole active
  lead only, no zero-lead fallback, fail closed on ambiguous multi-lead.

### Notes
- Honest scope: v1 is *prompt/session freshness*, not a hard isolation boundary
  (a frozen worktree + filtered bus view would be; deferred). Reviewed code and
  the marker prompt are untrusted data. Counted-sign-off mode is intentionally
  deferred.

### Upgrade
```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.36.0"
```
Additive and opt-in; ephemeral reviewers stay disabled until you enable them in
the supervisor config.

## [0.35.0] - 2026-06-27

The assurance review/test skill pack (assurance P4). Five new dev-discipline
skills — installed to both Claude and Codex by `install-skills` — give reviewers
and testers ready-made, evidence-honest rubrics that emit the typed
`review-result` the gate/close consume. Generic skeletons; projects supply their
own domain checklists.

### Added
- **Five devkit skills** (installed to `~/.claude/skills/` and `~/.codex/skills/`):
  - **`review-failure-injection`** — adversarial review of parsers, IO,
    persistence, untrusted input, resource limits, and cleanup/teardown.
  - **`review-contract-drift`** — schema/settings/renderers/tests/docs parity
    when a feature is retired, renamed, or reconfigured.
  - **`review-release-readiness`** — release-gate review (CI triggers, artifact
    type, manifest/permissions, gates, docs/version drift) → HOLD/GO.
  - **`system-review-protocol`** — a narrow-trigger milestone/full-repo
    adversarial review that coordinates lenses → ACCEPT/COUNTER/NA → draft close
    → remediation through the existing `agenttalk close` flow.
  - **`tester-qa`** — a first-class QA persona that writes/runs/triages tests and
    reports *actually executed* evidence (read-only against production in review
    mode; not a coverage-percentage rubric).
- Every skill carries a shared **evidence-honesty contract**: `tests_executed`
  means an actual command + result/exit (or CI run id), never inspected-only;
  release-blocking claims anchor to `automation_ci`; a skill picks one primary
  `risk_class` but lists all touched classes (the lead-owned close risk inventory
  stays authoritative for routing).

### Notes
- Generic and opt-in: agenttalk ships the skill skeletons + the evidence rules;
  projects supply domain-specific checklists, `signoffs.json`, and CI gates. This
  completes the assurance arc's review layer (P4). Next on the roadmap: ephemeral
  reviewers and the lane deliver-gate.

### Upgrade
```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.35.0"
agenttalk install-skills   # picks up the new assurance review/test pack
```

## [0.34.0] - 2026-06-27

Specialist sign-off by risk class (assurance P3). A milestone close can now
**derive its required reviewer sign-offs from the risk classes in play** —
routed to the right specialists by roster role/group (and domain reviewers) — and
won't go GREEN until enough distinct qualified reviewers have signed. Opt-in; the
project owns the risk→reviewer policy.

### Added
- **`agenttalk close signoffs` (plan / apply / override)** + `close open
  --derive-signoffs` — derive a close's required specialist sign-offs from a
  project signoff policy (`.agenttalk/signoffs.json`; missing = empty/opt-in) and
  the close's risk inventory:
  - **risk inventory** per close — `risk_class` validated against a core envelope
    (`none|unknown|release|security|performance|persistence|docs-contract|quality`)
    plus `project:` extensions; changed paths default from the frozen revision
    (`git diff`), with audited manual overrides.
  - **route freeze** stores policy/risk/revision **hashes + refsets**, never
    concrete people — candidates re-resolve against the current
    roster/groups/roles + domain reviewers at check time (a role change is honored
    without reopening; a policy/risk/revision change holds `stale_signoff_route`
    until re-applied).
  - **count semantics** — a set is satisfied only by enough **distinct qualifying
    agents** with countable acks (default `accept`); `NA` counts only if allowed
    (with a reason); `counter` and lead `--override` don't count unless opted in
    (and an override is surfaced); one agent can't satisfy `count=2`.
  - **`signoffs override`** is a close-lead-only escape for an unroutable
    requirement (fail-closed).
  - new HOLD codes: `missing_required_signoff`, `unroutable_required_signoff`,
    `invalid_signoff_policy`, `unmapped_required_risk`, `stale_signoff_route`.
- Reuses `.agenttalk/domains.json` reviewers as an **additive** candidate source;
  lens authorization gains `allowed_groups` so the refset vocabulary
  (agents/groups/roles) matches roster and domains.

### Notes
- Opt-in and generic: the project decides risk classes, the risk→sign-off
  mapping, required counts, and candidate refsets; agenttalk core owns the schema,
  refset resolution, pure verdict, and count enforcement (`compute_verdict` stays
  pure — the CLI supplies resolved candidate sets + hashes). This is P3 of the
  assurance roadmap; review rubrics and a tester skill are next (P4).

### Upgrade
```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.34.0"
```
Additive and opt-in; no migration. Create `.agenttalk/signoffs.json` to use
derived specialist sign-offs.

## [0.33.0] - 2026-06-27

The milestone-close protocol — the assurance spine's HOLD/GO gate for a whole
release or mission. `agenttalk close` aggregates gate state, typed review
evidence, required-lens sign-offs, counters, and remediation into one durable,
audited verdict, so a release can't quietly close while a blocker is red, a
required reviewer hasn't signed, or an accepted finding has no fix.

### Added
- **`agenttalk close` (open / ack / draft / counter decide / check / publish /
  reopen / list / show)** — a per-close, atomic, fail-closed record
  (`.agenttalk/closes/<id>.json`) that freezes the revision under review (ref →
  full SHA; a revision change stales prior sign-offs) and aggregates the release
  decision:
  - **required lenses** collect `ACCEPT | COUNTER | NA` (NA needs a reason); an
    ack is rejected unless the sender is authorized for that lens — a non-lead
    `--override` is ignored (override is a close-lead privilege, fail-closed);
  - **counters** must each be accepted or rejected by the lead with a reason; an
    accepted blocker finding becomes a **remediation item that must name a
    gate**, and GO requires that gate green (from CI) or waived;
  - **`close check`** is a pure verdict over close + gate state with stable HOLD
    codes, printing `HOLD`/`GO` and an automation-gateable exit (0=GO, 3=HOLD);
  - **`close publish --verdict go --bump-barrier`** records the GO durably, then
    fires the global release barrier (HOLD never bumps; post-publish acks are
    rejected unless reopened — stale-proof without a team-wide bump).
- Reuses the 0.32.0 gate/evidence + the epoch barrier; never creates or mutates
  gates itself.

### Notes
- Generic and opt-in: which gates and lenses are required, what a lens means,
  and severity policy are the project's to define; agenttalk core owns the
  schema, validation, pure verdict, and the advisory authority checks. Coexists
  with spec-kitty (wraps release/milestone confidence around it). This is P2 of
  the assurance roadmap; specialist sign-off routing and review rubrics are next.

### Upgrade
```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.33.0"
```
Additive and opt-in; no migration.

## [0.32.0] - 2026-06-27

The assurance spine — a lightweight layer that makes unsafe closure hard.
agenttalk now records durable, queryable gate state and typed review evidence, so
a release/merge/mission-close can be held while a blocker is red, and "looks fine
/ verified" prose can no longer pass for proof. Generic and opt-in: projects
declare which gates and risk classes matter; agenttalk core provides the
mechanism. Born from a field retrospective after an independent review caught
issues a multi-agent build had shipped past.

### Added
- **`agenttalk gate` (set / list / check / waive)** — durable, queryable gate
  state. A `severity=blocker` gate that is `red`/`unknown` yields **HOLD** and
  blocks release-like closure until it goes green or is waived. A blocker can be
  set **green only from `automation_ci` evidence** (or an audited operator
  waiver) — manual assertion is rejected. `gate check [--release]` prints a
  top-line `HOLD`/`GO` with an automation-gateable exit code (0=GO, 3=HOLD).
  `gate waive` records an operator waiver (operator, reason, scope, expiration,
  evidence); expired/wrong-scope waivers don't clear the gate. Gate state is
  scoped to revision so stale evidence can't satisfy a current gate.
- **`agenttalk check --gates`** — the pre-irreversible-action check now folds in
  gate state alongside request-currentness/epoch, so a blocked release/merge
  surfaces HOLD before you act.
- **Typed evidence on `review-result`** — approvals carry structured fields
  (`risk_class`, `release_blocker`, `tests_referenced` vs `tests_executed`,
  evidence/artifacts with source type, `residual_risk`, `na_reason`). Missing
  required fields make a result **incomplete, not approved**; `tests_referenced`
  can't be passed off as `tests_executed`; NA needs a reason. Lightweight reviews
  stay frictionless via `risk_class=none`.

### Notes
- **Opt-in and generic.** The required-gates set is empty by default — you only
  get gating where you declare it. Project-specific gates, evidence commands, and
  specialist lenses live in your project's config; agenttalk core provides the
  mechanism. This is the MVP slice; milestone-close, specialist sign-off routing,
  and the generic review rubrics are planned follow-ups.

### Upgrade
```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.32.0"
```
No migration; the gate/evidence layer is additive and opt-in.

## [0.31.2] - 2026-06-27

Supervised-wake reliability — the end of the 0.31.x supervisor line. A
full-team simultaneous wake (e.g. an outage that restarts everything at once)
no longer churns windows when resumed agents fail to re-enter their listen loop,
and wrapped agents are now the recommended default for hands-off supervision.

### Added
- **Readiness give-up cap.** A supervised agent that launches but never reaches
  its first heartbeat (e.g. a resumed manual agent that doesn't re-enter the
  listen loop) is retried at most `max_readiness_retries` times (default 3),
  then the supervisor STOPS relaunching it and surfaces a sticky
  `READINESS_GAVE_UP` warning (no kill; manual intervention) instead of churning
  windows forever. This readiness counter is separate from stuck-recovery
  backoff and resets on the first fresh heartbeat or an operator restart.
  Previously a never-ready resume could relaunch indefinitely.

### Changed
- **Wrapped is now the recommended/default supervised archetype.** The
  supervisor template and tutorial steer hands-off agents to `wrapped: true`
  (the wrapper owns the listen loop + heartbeat, so a resumed agent re-enters by
  construction and needs no activity hook); manual non-wrapped resume is
  documented as best-effort/legacy, protected by the readiness cap.
- **Wrapped Codex child gets `--disable hooks` by default** so a stray project
  `.codex/hooks.json` cannot make the wrapped child prompt for hook-trust
  (operator-overridable).

### Upgrade
```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.31.2"
```
Regenerate the supervisor scaffold with `agenttalk supervise --init --force` to
pick up the wrapped-as-default template. For hands-off supervision, prefer
`wrapped: true`.

## [0.31.1] - 2026-06-27

Supervisor robustness for the current Codex CLI, plus a non-blocking activity
hook. A field deployment surfaced two issues under unattended supervision; this
hardens both. No behavior change for wrapped agents or Claude.

### Fixed
- **Codex launch preflight no longer depends on the volatile `codex sandbox`
  CLI.** The non-wrapped Codex preflight previously ran `codex sandbox
  -P :workspace ...`, which fails on Codex builds that restructured the
  `sandbox` subcommand (platform subcommand required, `-P` dropped),
  fail-closing a healthy agent. It now runs the same plain `python -m agenttalk
  --version` import gate the Claude/generic path uses, under the seeded
  `CODEX_HOME` + `PYTHONPATH`; the seeded config remains the runtime sandbox
  authority. (Stable npm codex was unaffected; this future-proofs CLI drift.)
- **`agenttalk heartbeat --hook` soft mode** — the activity-hook command never
  blocks a tool call. As a PostToolUse hook with unresolved/off-roster identity
  it exits 0 silently (no stdout/stderr); the manual `agenttalk heartbeat` stays
  strict (exit 2). `install-activity-hook` now installs `agenttalk heartbeat
  --hook` and upgrades/dedupes legacy bare entries in `.claude/settings.json`
  and `.codex/hooks.json`.

### Added
- **`agenttalk doctor` surfaces the resolved supervised Codex path +
  `codex --version`**, with a best-effort non-blocking sandbox probe that hints
  on failure (you may be on an old/alpha/MS-Store codex; agenttalk expects the
  npm stable). Makes a wrong-executable pick visible instead of a cryptic
  fail-closed.
- **Non-wrapped supervised Codex with `activity_hook=true` launches with
  `--dangerously-bypass-hook-trust`** so the unattended agent does not strand at
  Codex's "Hooks need review" prompt (which re-triggers when the hook command
  changes). Scoped to that case only; wrapped/no-hook/Claude agents are
  unaffected — a deliberate trust bypass for the supervisor's own agenttalk hook
  in controlled unattended supervision.

### Upgrade
```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.31.1"
```
Re-run `agenttalk install-skills` (or `supervise --install-activity-hook`) to
pick up `agenttalk heartbeat --hook`; the installer upgrades legacy bare hook
entries automatically. For hands-off unattended supervision, prefer wrapped
agents (`wrapped: true`).

## [0.31.0] - 2026-06-26

The domain registry foundation, plus the documentation for the supervisor
and the progress wrapper. This is Phase 0 of a unified ownership middle
tier: a single project-local registry that the upcoming lane and knowledge
layers will share, instead of each inventing its own ownership model. Also
adds the supervisor/wrapper tutorial and README coverage that 0.30.0
shipped without.

### Added
- **`agenttalk domain` — the shared ownership registry (foundation).** A
  project-local `.agenttalk/domains.json` maps named domains to owners,
  reviewers, curators, and owned globs, plus a shared-path policy; refs are
  resolved against the roster (agents/groups/roles). Read-only commands:
  `domain list` (domains + a stable, key-order-independent registry hash),
  `domain show <id>` (one domain with resolved refs), `domain check-path
  <paths...>` (classify repo-relative paths as owned/unowned/shared, with
  `--case-sensitive`/`--case-insensitive`), and `domain validate`. The
  registry is preserved across `agenttalk reset` (it is config-like, not
  active bus state). The registry hash is the staleness keystone the later
  lane/knowledge phases stamp into their records. No mutation command yet —
  author the JSON by hand.
- **Supervisor + wrapper documentation.** A new README section ("Unattended
  operation: the supervisor and the wrapper") and a full step-by-step
  tutorial at `docs/supervisor-tutorial.md`: the heartbeat-liveness model,
  scaffolding and filling `supervisor.json` (manual and wrapped archetypes),
  the activity hook, running the monitor, restart-with-context (all four
  resume paths), the wrapper standalone, migrating a project in and out of
  supervision, and the safety/limitations. Adds the previously-undocumented
  `agenttalk wrap` to the CLI reference, plus README coverage of the new
  domain registry.

### Upgrade
```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.31.0"
```
No migration. The domain registry is opt-in — create `.agenttalk/domains.json`
to use it. The supervisor and wrapper are unchanged from 0.30.0; this release
documents them.

## [0.30.0] - 2026-06-26

The progress-adapter wrapper. Supervised agents can now run through
`agenttalk wrap`, which makes an agent VISIBLE (renders its turn to the
console), keeps its heartbeat fresh while it WORKS (not just while idle),
detects degraded output, and lets the supervisor auto-restart a crashed or hung
agent WITH its session context intact. Validated end-to-end live on Windows
(Codex and Claude), including a kill-and-resume-with-memory test. Also bundles
the court-leak listen-skill fix. Manual `/agenttalk.listen` stays the default;
the wrapper is strictly opt-in per agent (`wrapped: true`).

### Added
- **`agenttalk wrap` — the progress-adapter wrapper.** Launches a CLI in
  structured-stream mode and, per event, stamps a throttled heartbeat, renders
  readable output to the console, and scans for degraded output (a tool-call
  leaked as text). Two adapters: Codex (`codex exec --json`, item-level events)
  and Claude (`-p --output-format stream-json`, token + thinking deltas — so a
  long pure-reasoning Claude turn stays live). One-shot mode (`wrap -- <argv>`)
  for visibility; long-running `--loop` mode for supervised agents.
- **Wrapper-owned listen loop (`wrap --loop`, opt-in `wrapped: true`).** The
  wrapper becomes the long-running supervised process: it owns the idle bus-wait
  + heartbeat and drives the CLI one turn per inbound message, with session
  continuity across turns AND across a supervisor relaunch (Codex `thread_id` /
  Claude `--session-id`/`--resume`, persisted atomically). The model is a pure
  per-turn handler; the wrapper owns message delivery and the cursor (no
  model-side inbox commands). The wrapper handles loop-exit on `release`/`end`.
- **Machine-readable receive API** (`agenttalk recv --json` + an in-process
  path) so the wrapper consumes the bus via structured records, never by parsing
  human-readable output. Mirrors `wait`'s exact global/scoped cursor semantics.
- **Supervisor launches wrapped agents through the wrapper.** A `wrapped: true`
  agent is launched as the real Python wrapper (`-m agenttalk wrap --for <a>
  --cli <cli> --loop -- <real CLI argv>`); the supervisor supervises the one
  long-lived wrapper process and retires brain-pid discovery for wrapped agents
  (the wrapper is the scoped root; heartbeat is the liveness authority). Per-CLI
  stale thresholds: wrapped Claude defaults to 180s, wrapped Codex to 900s (with
  a <600s guardrail), because Codex's item-level stream is silent during long
  reasoning while Claude's deltas keep it fresh. The supervisor console and
  `supervise --report` use the per-CLI threshold (report parity).

### Fixed
- **Listen skill: a backgrounded `wait` that exits 1 is a clean timeout, not a
  failure** — re-arm; do not read the background task's output file (the read
  path where the intermittent "court" tool-call-leaked-as-text degraded output
  surfaced). Both Claude and Codex listen variants.
- **Wrapper decodes child stdout as UTF-8** (`errors="replace"`) instead of the
  Windows cp1252 default, so an agent emitting a smart quote or em-dash no longer
  crashes the wrapper.

### Known limitations
- Wrapped Codex uses a conservative stale threshold (default 900s) because no
  `codex exec --json` event marks progress during pure reasoning, so a genuine
  hang is detected only after that window. Wrapped Claude is tighter (180s).
- A poison inbound message that reliably fails a turn produces a
  backoff-throttled restart loop until operator/dead-letter intervention
  (a dead-letter / skip-after-N path is a planned follow-up).
- After a turn the wrapper waits silently (no explicit "idle" line); a clearer
  idle signal is a planned follow-up.

### Upgrade
```powershell
python -m pip install "git+https://github.com/zoolok17/agenttalk.git@v0.30.0"
```
Regenerate the supervisor scaffold with `agenttalk supervise --init --force` to
pick up the wrapped-agent template and the wrapped preflight. Manual agents are
unaffected; opt into the wrapper per agent with `wrapped: true`.

## [0.29.0] - 2026-06-22

Supervisor observability: the supervisor now shows its decisions in its own
console, so an operator can watch what it's doing instead of staring at a
silent window.

### Added
- **Supervisor console action log.** The generated `supervisor.ps1` prints a
  concise line for each agent as it polls: real actions (relaunch, stuck
  recovery, warnings, refusals) always print with their reason; steady
  healthy/no-action states print only on change; a periodic "N/M healthy"
  summary shows the loop is alive without flooding. A new `-Quiet` switch
  silences it (including helper warnings); `-DryRun` still prints every agent.

## [0.28.1] - 2026-06-22

Makes the v0.28.0 supervisor actually work end-to-end for an UNATTENDED agent
on Windows, plus a roster-safety fix. Hardened across six live kill-and-recover
tests on a real Windows box (each surfaced a distinct real-world failure); the
final cycle — launch with no UAC, reach the listen loop, stay healthy, get
killed, and auto-relaunch with context — passes.

### Fixed
- **Supervisor liveness now uses a UTC clock.** The generated `supervisor.ps1`
  derived "now" from `Get-Date -UFormat %s`, which on Windows PowerShell 5.1 is
  a LOCAL-time epoch. Heartbeats are stamped UTC, so on any non-UTC machine the
  supervisor read every heartbeat as stale by the timezone offset and
  false-killed healthy agents. Now uses
  `[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()`.
- **Launch layer reworked** so a supervised agent can actually start: launch the
  real CLI executable (never a `.cmd`/npm/PowerShell shim that hands off and
  exits), PATH-independent bus calls via a generated shim, Windows argument
  quoting, and an ASCII-only `supervisor.ps1` written UTF-8-with-BOM so PS 5.1
  parses it.
- **Unattended auto-mode.** The supervisor seeds each agent to launch in a
  never-prompt, never-elevate mode (no human to approve a prompt/UAC): Codex
  gets a seeded isolated `CODEX_HOME` (`approval_policy=never`,
  `[windows] sandbox=unelevated` — avoids the per-home admin/UAC install,
  workspace-write), Claude gets `--permission-mode bypassPermissions` + a seeded
  `.claude/settings.json`. A preflight smoke-test fails closed on a bad config
  instead of relaunch-storming.
- **In-sandbox bus access.** Listen/lead skills invoke `python -m agenttalk`
  (bare `python` + `src` on `PYTHONPATH`) so the bus works inside the Codex
  workspace sandbox, where the bare `agenttalk` app-execution-alias is denied.
- **Atomic writes survive the sandbox.** `_atomic.write_text` keeps the atomic
  temp+rename everywhere it works, but on a persistent Windows `os.replace`
  PermissionError (the Codex sandbox holds a write handle for the process
  lifetime) falls back to a direct final-path write. Covers every bus write a
  sandboxed agent makes (publish, cursor advance, heartbeat, waiting).
- **Heartbeat-staleness liveness.** Replaced fragile process-tree/PID "brain"
  discovery (which mis-identified Codex's forking launcher and false-killed
  healthy agents) with heartbeat freshness as the liveness authority — fresh
  means healthy; stale (with the activity hook on) means restart. Fails safe:
  it can miss a dead agent (a no-op) but never kills a healthy one.

### Added
- **`roster add --unique`.** Refuses (exit 3) when the name is an active
  identity and suggests a free variant, so a joining agent never silently
  re-binds someone else's identity. Plain `roster add` stays idempotent (warns
  on an active re-bind). Listen/lead skills pick a unique name on self-join.

## [0.28.0] - 2026-06-17

Two reliability features for long-running multi-agent teams: an explicit
"stand down" signal so a listener is never stopped by accident, and an
external supervisor that auto-recovers agents through server-side outages.

### Added
- **`release` signal + `agenttalk release`.** A dedicated control kind that
  tells a listener to exit its loop — distinct from `end` (which also exports
  a transcript). It's the ONLY thing besides `end` that stops a listener;
  every other message means "work done for now, keep listening". `agenttalk
  release --from A --to B` targets one agent; `--to-group`/`--all` stand down a
  team. A listener obeys `release` only from an `operator_facing`/lead sender
  (fail-closed on ambiguous leadership); release from anyone else is reported
  and the listener keeps listening. No transcript is exported.
- **`agenttalk supervise` — external agent supervisor.** `supervise --init`
  generates a per-team `supervisor.json` + a PowerShell supervisor script that
  launches each agent and monitors it. It **auto-restarts** an agent when the
  process it launched **dies**, and — when the activity hook is installed —
  when the agent **hangs** on an API error (alive but not progressing). Every
  relaunch **resumes the agent's session with full context** (Claude
  `--session-id`/`--resume`; Codex `resume --last`). Per-agent exponential
  backoff (so a real outage doesn't hot-loop), and the human-facing lead is
  protected (warn-only, never auto-killed). The Python core stays thin and
  stdlib-only; the generated script owns terminal launch/kill.
- **`agenttalk request-restart --for <agent>`.** Lead/operator-triggered
  restart: writes a marker the supervisor consumes (kill-if-alive + resume).
  `--force-protected` is required to restart a protected agent.
- **`agenttalk heartbeat` + activity hook.** A throttled liveness stamp the
  supervisor reads. Installed as a PostToolUse hook (`supervise
  --install-activity-hook`, Claude `.claude/settings.json` or Codex
  `.codex/hooks.json`, merge-safe), it keeps the heartbeat fresh *at tool
  boundaries* while working (plus the wait-loop heartbeat while idle), so a
  stale heartbeat reliably means "stuck" — distinguishing a hung agent from a
  busy one. Stuck-recovery (the only path that auto-kills an alive agent)
  fires ONLY when the activity hook is enabled; otherwise a stale heartbeat is
  warn-only, never a kill.

### Notes
- The supervisor replaces the manual "tell each agent who it is + start the
  listen loop" step: the launch command is non-interactive (identity via
  `AGENTTALK_SELF`, listen skill auto-invoked). Launch commands are
  operator-filled in `supervisor.json` (examples provided).
- v1 ships the PowerShell supervisor; a POSIX bash supervisor is a follow-up
  (the Python core is already cross-platform). Codex resume is `resume --last`
  (best-effort) until a real Codex session id is captured.

## [0.27.0] - 2026-06-15

Performance: eliminate the multi-day, machine-wide slowdown caused by
`agenttalk wait` polling. The per-poll cost was `O(waiters × store-size ×
poll-rate)` — every poll re-read and re-validated the entire `messages/`
store, the store never shrank, and dead waiters never stopped polling. Four
changes attack each factor; combined they keep an idle multi-agent bus cheap
indefinitely. All changes preserve existing delivery, validation, signing,
cursor/thread, epoch, and rescind semantics.

### Fixed
- **`wait` no longer re-parses the whole store every poll.** The poll now
  reads incrementally from the agent's cursor — only message files newer than
  the last-seen id are opened/parsed/validated. Per-poll cost drops from
  `O(store)` to `~O(new messages)` (≈18× faster at 800 messages, and flat as
  the store grows). The `since_id` floor is delivery-only; `valid_messages()`,
  epoch/thread/rescind derivation, and the invalid-file report still scan the
  full log.

### Added
- **Adaptive poll backoff.** An idle `wait` backs its poll interval off
  (doubling up to a cap) and snaps back to the base interval the instant any
  activity (message, composing, rescind) appears. Every sleep is clamped to the
  deadline and heartbeat interval, so timeouts and liveness are unaffected.
  Configurable via `--max-poll-interval` (default 2.0s; set at/below
  `--interval` to disable). Cuts idle polling ~6× (≈200→32 polls/min/waiter).
- **`agenttalk compact`.** Bounds live-store growth via conservative prefix
  compaction: archives only a contiguous prefix of delivery-valid messages
  below a safe `keep_floor` (the minimum of every active cursor, the current
  epoch barrier, the earliest message of any non-closed thread group, and a
  keep-tail of recent history). Never archives invalid/tamper files (they stay
  visible to `status`/`doctor`/`prune`). Manual command is always available
  (`--dry-run`, `--keep-count`, `--keep-age-days`, `--json`); an opportunistic
  auto-trigger at wait-arm is **off by default** (`compact.enabled`), threshold-
  and throttle-gated, never a background daemon. Archived messages move to
  `archived/compacted/` (cold storage). Config: `compact.enabled`,
  `compact.keep_count` (1000), `compact.keep_age_days` (30),
  `compact.trigger_threshold` (1200), `compact.min_interval_seconds` (3600).
- **Stale-waiter handling at `wait` arm.** Confirmed-dead `.waiting` markers are
  cleared; a soft-cap warning fires when live waiters exceed a threshold; and an
  opt-in `--refuse-stacked-wait` refuses to arm a second live waiter for the
  same agent (exit **6**). The default remains a non-fatal warning — duplicate
  detection is advisory, not a transport lock.

### Notes
- **Retention boundary.** Cold-archived *closed* history is no longer
  derivable, so `check --to-request` on a historical closed request may return
  `unknown` (exit 4) after compaction — safe and fail-closed, but not
  byte-identical history. Live derivations are unchanged.
- Capacity/headroom snapshots, advisory semantics, and the privacy boundary are
  unchanged.

## [0.26.0] - 2026-06-09

Context-aware coordination phase 1: capacity snapshots now include the
agent's local context-window fill so leads can avoid assigning long,
context-heavy work to agents near compaction.

### Added
- **Context headroom in `agenttalk capacity`.** Snapshots now carry optional
  `context_used_percent`, `context_window_size`, and `context_tokens` fields.
  `capacity show` prints a `context N%` segment and flags near-compaction
  snapshots with `--context-threshold` (default 80).
- **Claude Code context parsing.** Claude refresh reads
  `context_window.used_percentage`, `context_window.context_window_size`, and
  input-side `context_window.current_usage` token counts from the same
  status-line input file used for rate-limit data. A status line with context
  but no `rate_limits` block still publishes.
- **Codex context parsing.** Codex refresh reads
  `payload.info.last_token_usage.input_tokens` and
  `payload.info.model_context_window` from the same rollout `token_count`
  records used for budget data. It uses current input tokens divided by the
  model context window, not cumulative `total_token_usage`. A rollout record
  with context data but no `rate_limits` block still publishes.
- **Lead skill guidance.** Bundled Claude and Codex lead skills now treat
  context headroom as a coarse planning hint alongside rate-limit budget:
  steer long work away from near-compaction agents, ask stale/unknown agents to
  refresh, and warn the operator when every plausible owner is low or near
  compaction.

### Unchanged
- Context headroom is advisory only. Missing, stale, unknown, or
  near-compaction snapshots never block protocol progress, review validity, or
  spec-kitty state transitions.
- The privacy boundary is unchanged: snapshots publish only derived numbers and
  non-secret labels, not raw rollout/status-line contents, prompts, session
  text, provider paths, token bodies, auth paths, or account identifiers.

## [0.25.1] - 2026-06-09

Patch release fixing capacity timestamp parsing on Python 3.10.

### Fixed
- **Codex capacity snapshots on Python 3.10.** `_normalize_ts` fed provider
  timestamps of variable sub-second precision (e.g. Codex's `"...:00.0Z"`)
  straight to `datetime.fromisoformat`, which on Python 3.10 accepts only
  3- or 6-digit fractions and raises `ValueError` — so a real Codex rollout
  yielded no capacity snapshot on 3.10. The fraction is now padded/truncated
  to 6 digits before parsing, so any precision parses on every supported
  Python. Advisory-only: the bug degraded gracefully (no snapshot) and never
  crashed or gated protocol progress.

## [0.25.0] - 2026-06-09

Budget-aware coordination: agents can self-publish a privacy-safe,
advisory snapshot of their local 5-hour and weekly rate-limit budget so
a lead can make better assignment choices during long team runs.

### Added
- **`agenttalk capacity refresh|show`.** `refresh` reads the current
  agent's local Claude Code or Codex budget signal and stores a normalized
  snapshot under `.agenttalk/state/`; `show` (the default) prints the
  team's published 5-hour/weekly usage, reset timing, stale/unknown
  confidence, and near-cap/reset-soon flags.
- **Codex rollout parsing.** Codex refresh reads local
  `~/.codex/sessions/**/rollout-*.jsonl`, prefers the current
  `CODEX_THREAD_ID`, scans for the last `payload.rate_limits` record, maps
  300-minute and 10080-minute windows to 5-hour and weekly slots, and uses
  the rollout record timestamp for staleness.
- **Claude Code status-line parsing.** Claude refresh reads
  `~/.claude/statusline-last-input.json` (or `--statusline-path`) and
  normalizes `rate_limits.five_hour` and `rate_limits.seven_day`.
- **Lead skill guidance.** Bundled Claude and Codex lead skills now tell a
  lead to refresh its own capacity, read team snapshots, steer long work
  away from near-cap agents, and warn the operator when every plausible
  owner is low or unknown.

### Unchanged
- Capacity is strictly advisory. Missing, stale, unknown, or high-usage
  snapshots never block protocol progress, review validity, or spec-kitty
  state transitions.
- Published snapshots contain only derived budget metadata: percentages,
  reset epochs, window lengths, source, confidence, and non-secret plan
  labels. Raw session files, prompts, auth paths, token bodies, account ids,
  and local provider paths stay local.

## [0.24.0] - 2026-06-08

Coordination polish: three multi-agent fixes surfaced by production feedback
(`agenttalk-improvements.md`, items 3.1 / 3.3 / 3.2) and design-reviewed with
the peer agent. All additive and backward-compatible.

### Added
- **Escalation falls back to the team lead.** When `agenttalk escalate` has no
  `operator_facing` liaison configured, it now routes the operator question to
  the team **lead** instead of refusing — so a reviewer needing a human ruling
  is never stranded. Resolution order is `--to` → liaison → the single lead →
  exit 2 (with a remediation naming both `roster set-operator-facing` and
  `roster set-role … lead`).
- **At-most-one-`lead` roster invariant.** `roster set-role <agent> lead` now
  atomically demotes any prior lead and promotes the new one in a single step
  (`demoted X, promoted Y to lead`) — no `--force`, no manual two-step. The
  comparison is case-insensitive; the role is stored verbatim.
- **`doctor` no-escalation-target check.** Warns when a multi-agent team has
  neither a liaison nor a lead, so the gap is visible before an escalation is
  attempted. Absent for solo rosters and when a liaison or lead exists.
- **`wk-` correlation id on `wake` messages.** A `wake` now carries a `wk-` id
  so a reply can echo it, ending the fragile habit of reusing the raw message
  id. An explicit `request_id` is still honored.
- **Owed-inbound pre-send warning.** Before sending a peer unrelated traffic
  while you owe them an open decision-request (a `proposal` or an operator
  escalation), `send` emits a soft, best-effort warning naming the owed id.
  Suppressed when replying on that same `request_id`; silent for non-decision
  traffic; never blocks or fails the send.

### Unchanged (honesty notes)
- The roster `lead` role and the `operator_facing` liaison stay **distinct**
  concepts: the liaison is still the primary escalation target, the lead is only
  the fallback. They are not merged.
- The invariant is **at-most-one** lead, not exactly-one — zero leads remains a
  valid state, and solo / symmetric-pair runs are never forced to have a lead.
- `escalate` still exits 2 when there is genuinely no target; message history,
  message schema, exit codes, and `store.OPENER_KINDS` (wake is not a thread
  opener) are unchanged.

## [0.23.1] - 2026-06-08

Patch release: restore Python 3.10-3.12 compatibility in the Codex config
editor. No behavior change on 3.13+.

### Fixed
- `codex_config` read the per-project config with `Path.read_text(newline="")`,
  but the `newline` keyword on `Path.read_text()` only exists in Python 3.13+.
  On 3.10-3.12 every `enable_project`/`disable_project` call raised
  `TypeError: read_text() got an unexpected keyword argument 'newline'`,
  breaking the feature and turning the CI test matrix red from 0.21.0 onward.
  Reads now go through `open(..., newline="")`, which is portable across all
  supported versions and still disables universal-newline translation so CRLF
  configs are preserved.

## [0.23.0] - 2026-06-08

Dev-discipline skill bundle: agenttalk now ships the five shared coding
discipline skills as version-controlled package content, so teams no longer
depend on untracked personal/global copies.

### Added
- Bundled the devkit skill pack under `src/agenttalk/skills/devkit/`:
  `craft-code`, `test-coverage`, `review-code`, `write-docs`, and
  `review-docs`, including `review-code/references/security.md`.
- `agenttalk install-skills --devkit-only` installs only the devkit pack to
  both `~/.claude/skills/` and `~/.codex/skills/`.
- `agenttalk doctor` now reports `devkit_skills`, surfacing full absence as an
  opt-out-friendly OK hint while warning on partial or stale devkit installs.

### Changed
- `agenttalk install-skills` now installs the devkit by default alongside the
  existing bus skills. Use `--no-devkit` to keep the old bus-only behavior.
- Added `--claude-skills-dir` and `--codex-skills-dir` overrides for testing or
  custom devkit install locations. The existing `--claude-only` and
  `--codex-only` flags continue to scope only the bus-skill side.

### Tests
- Added installer coverage for devkit package presence, default library
  behavior, both-agent install layout, `--devkit-only`, `--no-devkit`, and
  devkit frontmatter.
- Added doctor coverage for absent, in-sync, partial, and stale devkit states.

## [0.22.0] - 2026-06-08

Review-fixes batch 3: low-risk runtime hardening, diagnostic accuracy, and
agent-facing docs/skills contract cleanup.

### Fixed
- Atomic writes are now crash-durable: temp files are fsynced before
  `os.replace`, POSIX parent directories are fsynced after the rename, and
  Windows replaces retry through transient sharing violations from concurrent
  readers. Transcript export now uses the shared atomic writer.
- `/api/state` now degrades to errors-as-data for any single-root collection
  failure, so one corrupt root cannot 500 the whole aggregate.
- Message delivery and thread derivation now fail closed on an empty or corrupt
  roster, delivering nothing instead of falling through to the old empty-roster
  fail-open path.
- `send -m ""` is treated as a deliberate empty body without stdin fallthrough,
  and `send --kind rescind` / `send --kind end` are rejected in favor of the
  dedicated commands.
- Broadcast not-applicable rollup is last-write-wins per member: a later
  substantive reply clears an earlier accidental `--na`.
- Dashboard loopback checks are now address-aware, rejecting non-loopback IPv6
  addresses that merely start with `::1` while accepting IPv4-mapped loopback.
  The server also binds a loopback literal and uses a per-server address family
  instead of mutating `ThreadingHTTPServer` globally.
- `doctor` now warns when a configured operator liaison has never listened, and
  the active-waiters diagnostic is explicit that PID reuse makes it advisory.

### Changed
- Proposal skills now point countered proposals at `meta.in_reply_to` only;
  the removed `meta.counter_request_id` field was never part of the CLI
  contract.
- Handoff skills now mint review-request ids with an `rq-` prefix, keeping
  them visually distinct from proposal ids (`pp-...`).
- Spec-kitty loop skills now use `AGENTTALK_MODEL_TAG` with conservative
  defaults instead of stale hard-coded model-version labels.
- The Codex consult skill now parses `agenttalk status --json` through stdin
  and no longer depends on `bc` for freshness checks.
- Codex listen review guidance now explicitly sends findings back to the
  implementer instead of rewriting a peer's patch during read-only review.
- `SECURITY.md` now states the unsigned-mode trust boundary directly and
  records non-wall-clock cursor / future-id handling as a future design item.

### Tests
- Added `tests/test_atomic.py` coverage for durable atomic writes, cleanup after
  write failure, Windows replace retry, and transcript atomicity.
- Added focused regressions for empty/corrupt-roster fail-closed delivery,
  `send -m ""`, `send --kind rescind/end` guards, broadcast `--na`
  last-write-wins, address-aware loopback checks, localhost literal binding,
  per-server address family, liaison-never-listened warnings, subject/meta HTML
  escaping, and unicode/CRLF multiline body round-trip.

## [0.21.0] - 2026-06-08

Review-fixes batch 2: medium-severity correctness and test-coverage fixes from
the v0.19.0 fresh-review pass. Implemented test-first and Codex-reviewed.

### Fixed
- `/api/state` now applies the same composing-marker freshness rule as the
  CLI: only `0 <= age <= COMPOSING_INTENT_STALE_SECONDS` is shown. Stale or
  future-dated composing markers no longer make the dashboard show abandoned
  writers as actively composing.
- Roster/admin config mutations are serialized with an `O_EXCL` sidecar lock
  around `config.json` read-modify-write operations. This prevents concurrent
  roster changes from silently losing updates, including retire/rename
  tombstones.
- Pairwise open-outbound thread hints no longer name a retired peer as
  `next_owner`. The thread remains visible, but the derived owner no longer
  points at a tombstone that cannot reply.
- `agenttalk codex-config --enable/--disable` now preserves CRLF line endings
  without double-translation, avoiding silent rewrites of Windows-style config
  files.

### Tests
- Added concurrent send coverage for in-process threads and cross-process
  writers to guard against message id/file clobbering.
- Added config-lock tests for live-holder timeout, dead-pid stale-lock cleanup,
  and concurrent roster updates without lost config writes.
- Added focused coverage for stale dashboard composing markers, retired-peer
  thread hints, and CRLF preservation in `codex-config`.

## [0.20.0] - 2026-06-08

Review-fixes batch 1: high-severity findings from the v0.19.0 fresh-review
pass, plus the independently converged HMAC key-health finding. Implemented
test-first and Codex-reviewed.

### Fixed
- Message files whose filename stem does not match the embedded `id` are now
  invalid/quarantinable instead of deliverable. This closes the
  low-sorting-filename / high-embedded-id cursor-poisoning vector.
- Validated message delivery and thread replay now explicitly sort by message
  id, restoring the documented chronological contract; duplicate ids are
  defensively de-duplicated.
- `agenttalk init` now honors the global root contract: `--path`/`--here`
  still wins, then global `--root`, then `$AGENTTALK_ROOT`, then CWD. This
  prevents accidental second-store creation when a root was pinned globally.
- HMAC key loading, inspection, and verification now reject empty, short, or
  garbage key files. `doctor` reports invalid keys as errors instead of
  falsely showing signing as enabled and healthy.

### Notes
- A correctly named future-dated message id can still expose the broader
  clock-skew/monotonic-cursor tradeoff in unsigned mode. That remains a
  separate design item; no ad hoc timestamp quarantine was added in this
  release.

## [0.19.0] - 2026-06-08

Dashboard polish release (issue #22): presentation upgrade on the existing
read-only `serve`/`dashboard` surface. Bus-native only, additive,
read-only — no new routes, no CSP change, no spec-kitty dependency. Design
Codex-accepted; per-WP reviewed.

### Added
- `/api/state` (additive; `schema_version` stays 1): per-agent `sent` and
  `received` message counts; per-root `edges` — an array of
  `{from, to, count}` directed traffic pairs (self-excluded, broadcast
  fan-out included, sorted by count desc, capped to the top 50 with
  additive `edges_truncated`/`edge_limit` when more pairs exist). Computed
  from the validated-message scan `/api/state` already performs (no extra
  walk).
- `/dashboard`: a **hierarchical team layout** — operator-facing liaison /
  lead on top, developer-ish roles left, reviewer-ish right (classified
  client-side from `role`/`operator_facing`); per-agent **cards** (name,
  role/groups, last-seen, sent, received, owes, composing); a
  **who-talks-to-whom** conversation panel from `edges`; a manual
  **Refresh** button and an **auto-refresh toggle** (on by default), wired
  with `addEventListener` (no inline handlers).

### Unchanged
- Routes, per-route CSP (script-capable only on `/dashboard`), exit codes,
  read-only guarantee (no-mutation regression still passes), loopback-only
  posture. Every prior `/api/state` key is intact; degraded roots keep the
  errors-as-data shape (no partial stats/edges).

### Notes
- Stats are bus-native only: the dashboard never imports spec-kitty and
  does not show token usage (not tracked by the bus — a deliberate honesty
  choice, like task-completion counts).

## [0.18.0] - 2026-06-07

Review-hardening release (issue #21): fixes from two independent
fresh-context full-codebase reviews — all cross-feature / cross-release
interactions the per-diff review loop had missed — plus one
operator-requested guardrail. Design Codex-accepted; per-WP reviewed.

### Fixed
- **BLOCKER:** a message file with a non-string `meta.signature` (signing
  enforced) made `hmac.compare_digest` raise an uncaught `TypeError` that
  crashed every read path — including `list_invalid_messages`, so the poison
  file could not even be quarantined. It is now treated as a normal invalid
  (quarantinable) message.
- A message whose `id` is not a generated-id shape (e.g. `"zzzz"`) is now
  rejected as invalid at scan time — previously it delivered and, once acked,
  poisoned the recipient's cursor so every later message was hidden.
- Retired identities' history no longer vanishes from `agenttalk tail` and the
  dashboard message routes (`/api/messages`, `/messages/<id>`, index) — both
  now validate against the known roster (active ∪ retired), matching
  `valid_messages` and the dashboard thread panel.
- `broadcast --resume` no longer gets permanently stuck (exit 5 forever) when a
  frozen recipient was retired after a partial fan-out: retired recipients are
  reported under `dropped` and skipped; an all-retired remainder resolves to
  exit 0. Exit 5 is now emitted only on a genuine active-copy failure.
- Broadcast obligation derivation no longer lists a retired audience member as
  an owed `await-reply` — across `threads`, `sync`, `status`, `whoami`, and the
  dashboard.

### Added
- `agenttalk wait`: advisory warning when another **live** process is already
  waiting as the same agent in the same store (one window per agent is the
  assumed model). Best-effort, never blocks, never changes the exit code.
- `agenttalk doctor`: reports the current `.waiting` marker PID + liveness per
  agent (advisory; not a complete duplicate check).
- Additive fields: broadcast threads carry `audience_retired` (retired members
  of the frozen audience); `broadcast --resume` manifests carry `dropped`.

### Docs / honesty
- README + SECURITY: one window per agent is unsupported-if-violated (warned,
  not enforced); synced multi-machine stores assume clock agreement (id-shape
  validation does not fix clock skew).

### Notes
- The dashboard/`tail` roster-parity fix is a visible change: retired
  identities' historical messages now render where they previously did not.
- Backward-compatible: new fields are additive (absent when unused); old
  on-disk marker/heartbeat formats still read.

## [0.17.0] - 2026-06-07

Obligation dashboard release (issue #20): the bus gets a glanceable,
multi-project, read-only web view of who is doing what — built by
extending the existing `agenttalk serve` server, not forking it.
Design converged over the bus itself (Claude proposal → Codex counter →
accepted) and was Codex-reviewed pre-code and per-WP.

### Added
- `agenttalk dashboard` — alias to the same loopback-only server as
  `serve`, landing on the new obligation view. Repeatable
  `--store <project-root>` watches several projects in one tab (each
  path IS the root — no upward walk; missing stores warn and render as
  degraded panels). Deliberately has **no `--host` option**.
- `GET /api/state` — purpose-built versioned aggregate
  (`schema_version: 1`): roots as an array of namespaced objects with
  roster/roles/liaison, retired tombstones, per-agent presence
  (heartbeat age, unread, composing — an array, matching the
  multi-entry marker), open-thread rows with `next_owner`/`next_action`,
  `mission`/`wp_id` from opener meta, `epoch_at_send` forwarded in its
  exact three-state form plus a derived `epoch_status`
  (`current` / `previous-epoch` / `unknown-pre-epoch`, the
  `check --epoch` vocabulary), broadcast manifests, counts, the current
  epoch, and spec-kitty link metadata when `kitty-specs/` exists
  (filesystem detection only). **No message bodies anywhere.**
- `GET /dashboard` + `GET /static/dashboard.js` — hierarchy HTML
  (liaison first), ~2 s auto-refresh via a self-hosted polling script;
  DOM built via `textContent` only; detail links only for the first
  root (existing `/messages/<id>` routes bind to root[0]).
- Per-root error isolation: a corrupt or uninitialized root degrades to
  an `errors` entry in the payload — never a 5xx, and it recovers on
  the next poll without a server restart.
- `/` (message log) gains a link to `/dashboard`.

### Changed
- `serve`/`dashboard` bind failures now exit **2** with an actionable
  message naming the host:port and suggesting `--port 0` (previously a
  raw `OSError` escaped to the generic handler — a real operator hit
  this as bare `[WinError 10013]` when an unrelated app held 8765).
- `make_server` accepts an additive keyword-only `extra=` list of root
  descriptors; the legacy single-store call shape is unchanged.

### Security
- Per-route CSP split: only `/dashboard` allows (self-hosted) script +
  same-origin fetch; the hostile-body routes keep the pre-0.17.0
  policy byte-identical — both literals pinned by test.
- Read-only proven by regression: a full-tree content-hash walk over
  two stores asserts byte-identical state after mixed dashboard
  traffic (hashes, not mtimes — Windows).
- Loopback wall unchanged: no auth, no remote-bind flag on any
  spelling; multi-root blast-radius framing documented in SECURITY.md.

### Performance
- `/api/state` costs ONE message-dir scan per root per request
  (naively stacking the store's surfaces measured 5+ scans and ~9.6 s
  at 1k messages; derivation itself profiles at ~5 ms).

## [0.16.0] - 2026-06-05

Trusted-team safety release: Phase A of the identity/authz RFC
(`docs/rfc-identity-authz.md`). Identity becomes a first-class registry
with permanent tombstones, rename/retire stop rewriting history, a
lightweight global-epoch barrier plus `check --epoch` give the team a
clean "void the previous run" boundary, and open threads expose who owes
the next move. **This is trusted-team safety, not authorization** — it
assumes a cooperative roster and does NOT defend against a local peer
that forges sends, edits `config.json`, or deletes messages (see
SECURITY.md; later RFC phases B/C/D add real authz and replay hardening).

### Added

- **Identity registry + retirement.** A `retired` list in `config.json`
  records permanent, non-rebindable tombstones. `agenttalk roster
  retire <name>` retires an agent (it can no longer send; its name can
  never be re-bound; its history stays valid). `roster rename <old>
  <new> [--drain-check]` is retirement-not-rewrite: it tombstones `<old>`
  (`renamed_to=<new>`), activates `<new>`, and carries over role / group
  / operator-facing; `--drain-check` refuses while work is owed to/from
  `<old>`. `roster remove` now refuses by default with a retire hint;
  `--force` removes anyway and warns that historical readability breaks
  (no tombstone — the name stays re-addable). Optional `roster forward
  <retired> --to <live> --to-request <rid>` redirects a single owed
  request, transcript-visible (`meta.forwarded_from` /
  `forwarded_request_id`), single hop only.
- **Global epoch barriers.** `agenttalk barrier bump --from <agent>
  --scope global -m "<reason>"` fires one ordinary meta-marked message
  (no new kind) whose message id becomes the global epoch. Any active
  member may bump (a deliberate trusted-team global-stall lever).
  Tracked openers automatically record `epoch_at_send` (three-state:
  absent on pre-0.16 openers, `null` when no barrier has fired yet, the
  barrier id once one has).
- **`check --epoch`.** Extends the pre-action gate with the epoch
  dimension: exit 0 current, exit 3 when the request predates the latest
  barrier (previous-epoch, or a pre-epoch opener that must be re-asked).
  Fails closed on the exit code so automation gating on it is safe.
  Fails OPEN against barrier *suppression* (a deleted barrier) — trusted
  team only, documented.
- **Tool-visible next move.** `threads --json` / `sync --json` open
  threads now carry read-only `next_owner` / `next_action`
  (`reply` / `read-reply` / `await-reply` / `answer-operator`), a pure
  projection of thread state — never settable, never affecting delivery
  or closure. Terminal threads omit them.
- **Doctor.** A new `identity_registry` check reports active/retired
  counts and flags a dangling rename lineage.

### Notes

- Strictly additive over 0.15.0: a store with no retirements, no
  barriers, and no epoch-aware openers behaves exactly as 0.15.0. New
  message-meta keys are ignored by old readers. The one deliberate
  absent-vs-null exception is `epoch_at_send` (see SECURITY.md).
- Broadcast openers snapshot the epoch once before fan-out, so every
  copy of one `broadcast_id` shares a single `epoch_at_send`.

## [0.15.0] - 2026-06-05

Team-scope release: the remaining friction cluster from the four-agent
production band. Reviewer-only questions stop obligating non-reviewers
(and the placeholder-ack workaround dies honestly), broadcast fan-out
stops failing silently partway, and the 562-INVALID-files class of
store debris becomes recoverable quarantine.

### Added

- **Role-scoped audiences.** `agenttalk broadcast --to-role <role>`
  targets every roster member holding a role (sibling resolver — no
  role/group fallback ambiguity; unknown/empty roles refuse loudly
  naming the known set). Every fan-out copy now freezes its audience
  facts at send time (`audience_kind`, `audience_resolved`,
  `batch_total`, `audience_role` for role targets): later roster
  changes never alter historical obligations.
- **Not-applicable replies.** `agenttalk reply --na` closes your
  obligation on a question thread with a structured n/a response,
  displayed distinctly (`na=[...]` / `(n/a)`) for both perspectives.
  Refused on review-request/proposal threads (typed responses
  required); mutually exclusive with --kind; default body without
  stdin sniffing.
- **Broadcast delivery accounting.** A mid-batch failure prints a
  `delivered=[...]` / `missed=[...]` manifest (human and `--json`) and
  exits **5** (new documented code). `agenttalk broadcast --resume
  <bid>` re-sends the missing copies from the frozen originals
  (broadcaster-only, no overrides, accountable itself). `status` warns
  `incomplete fan-out` naming the missed members until resumed or
  rescinded.
- **Quarantine.** `agenttalk prune --invalid [--dry-run] [--json]`
  moves validation-failing files to `.agenttalk/quarantine/` —
  move-only, collision-suffixed, recoverable; selection is the exact
  INVALID gate walk, path-paired at scan time so valid files are
  unselectable by construction. `status` gains a `quarantined` count;
  `doctor` gains a store-hygiene check (inspect-first remediation).

### Changed

- `threads`/`sync` display n/a responders and frozen batch facts
  (additive keys: `responded_na`, `na_response`, `batch_total`,
  `audience_kind`). Bundled skills (both CLIs) teach: reply --na
  instead of placeholder acks; prefer --to-role; recover exit-5
  batches with --resume or rescind; prune with --dry-run first.

### Security

- No trust-model change. Quarantine selection is path-paired (an
  embedded-id collision cannot move a valid file — regression-tested);
  frozen audience/batch meta is untrusted display data (obligations
  derive from the validated copies); `--na` is an ordinary validated
  reply. See SECURITY.md.

## [0.14.0] - 2026-06-05

Operator-safety release, from the four-agent production band's second
retro (2026-06-05) plus a direct operator requirement. Closes the two
incidents that survived 0.13.0: a launch HOLD that crossed mid-flight
with its "fire" message (a voided run), and two terminal windows
silently talking to two different stores. Adds the operator-liaison
workflow: one designated agent the human talks to, with a loud,
correlated escalation path for everyone else.

### Added

- **Rescind.** `agenttalk rescind --from A --to-request RID [--to-id MSG]
  [-m reason]` marks a tracked request you opened as no-longer-current.
  First-class and transcript-visible (in the known kinds, NOT a hidden
  control kind); thread derivation reports `closed-superseded` for every
  participant; the first qualifying rescind decides and later duplicates
  are audit-only; requester-only. A re-ask after a rescind needs a fresh
  request_id. A per-agent manual `ack` keeps its own `closed` label —
  view closure never masks the fact.
- **Pre-action currentness gate.** `agenttalk check --for A --to-request
  RID [--json]` prints `current`/`superseded`/`unknown` and exits 0/3/4.
  The contract for irreversible actions: run it immediately before
  acting on a request you drained earlier — the executor-already-read
  race cannot be closed by any inbox primitive, only by this gate.
  Read-only and ack-independent.
- **Rescinded wake.** A scoped `wait --to-request` on a rescinded
  request wakes immediately (at entry or mid-wait) with a `RESCINDED`
  banner and **exit 3**, instead of blocking for a reply that should
  never come. Exit 1 remains exclusively the wait timeout.
- **Operator liaison.** `agenttalk roster set-operator-facing <name>`
  (single slot — "two liaisons" is unrepresentable; `--clear` removes)
  designates the one agent the human operator talks to directly.
  `agenttalk escalate --from W -m "..."` routes an operator question to
  the liaison automatically as a tracked question (`esc-` request_id,
  printed as `request_id=<id>`), and refuses loudly (exit 2, with
  remediation) when no liaison is resolvable. The liaison's `sync` shows
  pending escalations under OPERATOR INPUT NEEDED; an answer on the same
  request_id clears them. Advisory routing metadata — not authorization.
- **Root hardening.** New `AGENTTALK_ROOT` env var with strict
  precedence (`--root` flag > env > upward walk; a pinned root that has
  no store fails loudly, never falls back). `agenttalk init` refuses to
  create a nested store when one exists up-tree (`--force` for a
  deliberate sandbox). `doctor` detects and names every store from the
  working directory upward, flags split-brain layouts, and both `doctor`
  and `whoami` lead with `root:` as their first line.
- **Liaison diagnostics.** `doctor` checks the operator-facing
  designation: unset-but-escalations-exist, configured-but-pruned
  (error), and stale-heartbeat liaison.
- **Reply-in-flight visibility.** `agenttalk composing --to-request RID`
  binds the ping to one thread (the counterparty is derived from the
  thread — single argument), extends the peer's scoped wait, and records
  an observational marker that `threads`/`sync` show as
  "(reply in flight)" — suppressing the stale-thread warning while a
  reply is being drafted. Allowed exactly when you owe the thread's next
  move (including the needs-info ping-pong).

### Changed

- `threads`/`sync`/`status` surface the new states: `SUPERSEDED` rows
  with rescind provenance (who/when/why), a RESCINDED section in `sync`
  for rescinds the agent has not yet consumed, the liaison's escalation
  bucket, `[operator-facing]` markers, and new warnings (stale pending
  escalations; escalations-with-no-liaison). All JSON additions are
  strictly additive: absent when the features are unused, never null.
- Bundled skills (both CLIs) teach the four new contracts: check before
  irreversible actions; rescind over prose retractions; escalate instead
  of asking your own window's human (with the liaison's single-voice
  rule); and `composing --to-request` while drafting long replies.
- The `composing` help text no longer claims a 240s default wait
  timeout (the actual default is 120s).

### Security

- No trust-model change. `rescind` is validated content (same roster/
  HMAC gates as any message; derivation honors only the requester's
  rescind); `check` answers from the validated log and cannot be masked
  by per-agent state; `operator_facing` is advisory routing metadata in
  config.json, explicitly not an authorization boundary; the
  reply-in-flight marker is observational with the heartbeat/waiting
  tamper profile. See SECURITY.md.

## [0.13.0] - 2026-06-03

Workflow-safety and Windows-ergonomics release for the remaining
production-retro rough edges: safer reply routing, robust body input,
and explicit identity diagnostics before agents act after a restart.

### Added

- **Reply dry-run.** `agenttalk reply --dry-run` resolves the reply
  anchor (`--to-id`, `--to-request`, or last received message), prints
  the would-be recipient, echoed `request_id`, and kind, then exits
  without sending. This makes broadcast/thread-originator routing
  inspectable before a reply is committed.
- **stdin body files.** `--file -` now reads the body from stdin across
  `send`, `reply`, `propose`, and `broadcast`, giving Windows users a
  reliable here-string-friendly path for multi-line text, apostrophes,
  backslashes, and paths.
- **Identity diagnostics.** `agenttalk whoami [--for A] [--json]`
  reports the effective root, resolved self and peer, roster
  membership, role/groups, unread count, and owed-thread count. It
  warns when identity is unset or the resolved agent is not in the
  roster, which usually points to a misplaced `--root` or env typo.

### Changed

- Bundled skills and README now teach the safer restart bootstrap:
  `agenttalk roster` -> `agenttalk status` -> `agenttalk sync --for A`
  before acting after a restart, context compaction, or long idle
  period.
- Invocation docs now emphasize that `--root <path>` is a global
  option and must precede the subcommand, and that env vars set inside
  one LLM tool-call shell may not persist into the next call.
- Windows body guidance now defaults to here-strings piped to
  `--file -`, with machine-readable roots, paths, request ids, and
  routing data carried in `--meta key=value` rather than prose alone.
- Lead/reviewer/liaison guidance now states the authority boundary:
  re-derive HOLD/GO, ownership, and pending-review state from the
  repository, operator, `sync`, `threads`, and spec-kitty when
  applicable, not from stale message-body prose after a restart.

## [0.12.1] - 2026-06-03

### Fixed

- **Scoped wait respects global consumption.** `agenttalk wait --to-request`
  no longer re-delivers a message already consumed through the global cursor
  by `drain` or plain `wait`. Scoped delivery now starts at
  `max(per-thread seen_msg_id, global cursor)`, so after draining and answering
  a needs-info request it awaits the next reply instead of re-showing the old
  one. Scoped wait remains non-consuming and advances only the per-thread
  pointer.

### Documentation

- Clarified the global-cursor vs per-thread-state model: `reply-waiting`
  derives from the global cursor; `threadstate.json` is created lazily by
  `wait --to-request` and `ack --to-request`, not at init; `ack --to-request`
  closure is permanent, and a re-ask needs a fresh `request_id`. SECURITY notes
  threadstate tampering has the same denial-of-service profile as cursor
  tampering. ROADMAP now reflects that 0.12.0 delivered the first three
  production-retro items.

## [0.12.0] - 2026-06-03

Coordination recovery release from the first four-agent production
retro. The goal is to make restarts and busy team inboxes recoverable:
agents can rejoin with a digest, wait on one known thread without
consuming unrelated traffic, and manually close handled threads when
the strict request/response contract was not enough.

### Added

- **Scoped wait.** `agenttalk wait --for A --to-request RID` and
  optional `--kind K` return only matching addressed messages. Scoped
  waits advance only the per-thread `seen_msg_id` pointer and never
  advance the global inbox cursor, so unrelated traffic stays unread.
- **Per-agent threadstate.** `.agenttalk/state/<agent>.threadstate.json`
  stores per-request `seen_msg_id` and `closed` state used by scoped
  waits and manual closure. This is additive and leaves existing
  cursor files backward-compatible.
- **Explicit thread closure.** `agenttalk ack --for A --to-request RID`
  manually marks a handled request thread closed for that agent
  without touching the global cursor.
- **Rejoin digest.** `agenttalk sync --for A [--json]` summarizes
  identity, roster, actionable request threads, terminal decisions,
  recent unread non-action traffic, and deterministic next-action
  hints for restart/context recovery.

### Changed

- **Question closure is broader.** Question-style threads, including
  broadcast questions, close when the expected counterparty sends any
  non-control response with the same `request_id`. Review requests
  still require `review-result`, and proposals still require
  `proposal-response`.
- **Bundled skills now use scoped waits for known threads.** Handoff,
  consult, propose, listen, sk-loop, and lead docs teach
  `wait --to-request`, `sync --for`, and `ack --to-request` so agents
  do not wake on unrelated traffic or assert stale state after a
  restart.
- README documents the new global-cursor vs per-thread-state mental
  model, sync workflow, and 0.12.0 CLI surface.

### Security

- No trust-model change. The new threadstate is local coordination
  metadata inside the existing `.agenttalk/` state directory; message
  validation and optional HMAC behavior are unchanged.

## [0.11.1] — 2026-06-02

Patch release from the fresh-reviewer experiment: Claude Code and
Codex each spawned a fresh sub-agent with no prior context to review
the v0.11.0 multi-agent release, then compared findings.

### Fixed

- **Broadcast thread detection is now stricter.** `agenttalk threads`
  no longer treats any opener carrying free-form `meta.audience` as a
  broadcast. A normal point-to-point `question`, `review-request`, or
  `proposal` with a stray `--meta audience=...` now stays on the
  pairwise path, so its expected response kind can close the thread.
  Broadcast derivation requires `kind=question` plus
  `meta.broadcast_id`, which the `agenttalk broadcast` command always
  sets.
- **Roster mutators tolerate null team maps.** A config containing
  explicit `groups: null` or `roles: null` no longer crashes roster
  admin operations with a `TypeError`. `roster set-role`, `set-group`,
  and `add --role/--group` now coerce null or absent team maps to an
  empty map before mutation.

## [0.11.0] — 2026-06-02

Adds the multi-agent team surface: roster roles/groups, broadcast
fan-out, multi-party thread tracking, and lead skills. This release is
designed around named local participants rather than process
supervision: agenttalk remains the file-backed bus, while humans or
external launchers start worker windows.

### Added

- **Roster roles and groups.** New `agenttalk roster` command shows
  agents, roles, group memberships, and resolved identity. Admin
  subcommands add/remove agents, set roles, and set named groups. The
  implicit `all` group is reserved.
- **Broadcast fan-out.** New `agenttalk broadcast --to-group <group>`
  / `--all` writes one message per recipient, excluding the sender.
  Broadcast `message`/`note` are FYI fan-out; broadcast `question`
  tracks one response obligation per recipient by reusing
  `meta.request_id` with a `b-...` broadcast id.
- **Multi-party thread state.** `agenttalk threads` recognizes
  broadcast questions, showing responded/pending recipients for the
  broadcaster and owed-inbound state for each recipient until they
  reply with `agenttalk reply --to-request <b-id>`.
- **Lead skills.** Added `/agenttalk.lead` for Claude Code and
  `$agenttalk-lead` for Codex. The lead coordinates named agents and
  groups using point-to-point sends, broadcast questions, and thread
  tracking. It never spawns worker processes and does not duplicate
  spec-kitty.

### Changed

- Bundled send/listen/handoff/consult/propose/sk-loop skills now
  generalize from a hardwired `SELF`/`PEER` pair to named targets and
  groups while preserving the `claude`/`codex` defaults.
- Listen skills document broadcast fan-out handling: answer broadcast
  questions back to the sender on the shared request id; do not
  reply-all unless explicitly asked.
- README documents team setup, fresh-review naming, broadcast/groups,
  and the lead role. SECURITY.md documents that roles/groups are
  routing metadata, not authorization boundaries.

### Security

- Team support does not change the trust model: rostered participants
  are still inside the same local trust boundary. Optional HMAC remains
  project-key based and does not provide per-agent crypto identity.

## [0.10.0] — 2026-06-02

Adds three coordination features developed jointly by Claude Code and
Codex over agenttalk itself (collaborative design, a split
implementation, and cross-review in both directions): thread tracking,
first-class proposals, and anchored replies.

### Added

- **Thread tracking.** New `agenttalk threads [--for A] [--all]
  [--json]` command derives request/reply state from validated
  messages only. Default output shows actionable rows
  (`reply-waiting`, `owed-inbound`, `open-outbound`); `--all` includes
  `closed`. `--json` provides stable `threads[]` and `counts` fields
  for skill automation. State is computed by a chronological
  ball-passing replay, so the review `needs-info` round-trip
  (reviewer → requester → reviewer) and the multi-round consult
  convention (a follow-up reusing the original `request_id`) are
  tracked correctly rather than getting stuck.
- **First-class proposals.** New `proposal` and `proposal-response`
  message kinds, plus `agenttalk propose`, support accept/reject/counter
  decision flow. Proposal correlation reuses `meta.request_id` with a
  `pp-` prefix; counters close the old proposal with
  `proposal-response status=countered` and open a fresh proposal with
  `--in-reply-to <old-request-id>`.
- **Anchored replies.** `agenttalk reply --to-id <message_id>` and
  `--to-request <request_id>` anchor a response to a specific received
  message or thread, avoiding accidental correlation to the latest
  unrelated inbox message.
- **Bundled proposal skills.** Added `/agenttalk.propose` for Claude
  Code and `$agenttalk-propose` for Codex.

### Changed

- `review-request`, `question`, and `proposal` now auto-mint
  `meta.request_id` when absent (`rq-`, `q-`, and `pp-` prefixes).
  Missing `request_id` warnings now cover both `review-result` and
  `proposal-response`.
- `agenttalk status` warnings now include unconsumed correlated replies
  and stale outbound threads, derived from the same thread helper as
  `agenttalk threads`.
- Bundled listen, handoff, and sk-loop skills now require
  `agenttalk threads --for <agent>` before declaring work done or going
  idle, document proposal/proposal-response handling, and explicitly
  state that proposals cannot bypass user approval for split work.
- The `send` skills (both sides) now frame `question` as a tracked
  `q-` thread (steering true fire-and-forget pings to
  `message`/`note`) and list the new proposal kinds, pointing at
  `agenttalk propose` as the intended entry point.

### Internal

- New `Store.valid_messages()` exposes the roster- and
  signature-validated message set (all recipients) that thread
  derivation consumes, so a forged or unsigned response can never
  falsely close a real thread. `messages_for` is refactored on top of
  the shared `_validated_messages()` gate.

## [0.9.0] — 2026-05-24

Minor release. Fixes the issue #5 coordination hiccup (jointly
reported by Claude + Codex): a two-agent **soft deadlock** where each
agent waited on the other. Root cause was discoverability — `recv` is
a non-consuming *peek*, but its name implies consumption, so one agent
never advanced its cursor (`unread` climbed to 30) and hand-rolled
fragile timestamp polling instead of using the built-in cursor. This
release makes the consume path obvious, makes the stall visible, and
tightens request↔result correlation.

### Added

- **`agenttalk drain --for <agent>`.** The single obvious "consume my
  inbox" verb: prints all unread AND advances the cursor to the newest
  message. Mechanically identical to `recv --ack` (shares the exact
  code path) and advances past hidden control messages too. Use this
  instead of hand-rolled polling.
- **`recv` peek hint.** Plain `recv` (no `--ack`, no `--since`, not
  `--quiet`) that prints messages now emits a one-line stderr hint that
  the cursor did NOT move and points at `drain` / `recv --ack`.
  Suppressed for `--ack` (consuming), `--since` (deliberate history
  inspection), and `--quiet`.
- **Mutual-wait / soft-deadlock detection.** `agenttalk wait` now
  writes an observational `.waiting` marker (pid, since, cursor,
  timeout, epoch deadline) while blocking and clears it on exit
  (message, timeout, or interrupt). `agenttalk status` flags a
  **soft-deadlock** when two or more agents are blocked in `wait`
  simultaneously, and warns when any agent has **unread but a
  never-set cursor**. The marker is strictly observational — like the
  heartbeat, nothing about delivery or correctness depends on it, and
  orphaned markers from crashed shells are detected as stale via
  heartbeat age + the recorded deadline.
- **Auto-correlation for review-requests.** `agenttalk send --kind
  review-request` (and `reply --kind review-request`) now mints a
  `request_id` into `meta` when one isn't supplied, and prints it.
  `review-result` without a `request_id` emits a soft stderr warning
  (exit code unchanged) so request↔result threading is enforced by
  convention without breaking mixed-version peers.

### Changed

- `agenttalk status --json` gains a top-level `warnings` array and
  per-agent `waiting` / `waiting_stale` fields. All pre-existing keys
  are preserved (additive change — existing consumers keep working).
- README + skill bodies document the cursor / `--ack` / `--since`
  mental model in one place: **`recv` peeks, `drain` (or `recv --ack`)
  consumes, `wait` consumes one real message, `--since` inspects
  history without moving the cursor.**
- Reconciled `pyproject.toml` version (was stale at 0.7.2) with the
  package `__version__`; both are now 0.9.0.

### Tests

- Added coverage for `drain` (consume-to-newest, hidden-control-only
  advance, `--include-control`, `--quiet`), the `recv` hint
  (fires/suppressed matrix), `.waiting` marker lifecycle (written
  mid-wait, cleared on message + timeout), `status` warnings
  (never-acked unread, live soft-deadlock, stale-marker exclusion,
  JSON schema), and request_id autogen + soft missing-result warning
  across both `send` and `reply`.

## [0.8.0] — 2026-05-22

Minor release. Fixes a real-world sharp edge: a peer's reply landed
~12 s after `agenttalk wait` timed out at 240 s, so the reply was
lost to the waiter even though it was already on disk. Adds two
mechanisms — a small post-timeout grace window for clock-jitter
cases, and a new `composing` control kind so a peer drafting a long
reply can keep the waiter's deadline alive on demand.

### Added

- **`composing` message kind.** New control-plane kind that the
  peer can send while drafting a long reply. `agenttalk wait`
  consumes each fresh composing ping as a deadline-extension signal
  (`--composing-extend SECONDS`, default 120 s; hard cap 1800 s
  total per wait) and does NOT surface composings as a returned
  reply. `agenttalk recv` hides composings from its default view
  (`--include-control` opts back in). Old receivers on
  ≤ 0.7.2 silently drop the unknown kind, so the feature degrades
  gracefully across mixed versions.
- **`agenttalk composing --to <peer>` subcommand.** One-line helper
  for the most common composing-ping flow. Same write path as
  `send`: validated, optionally HMAC-signed, logged in the
  transcript and dashboard. Use periodically while drafting:
  ```powershell
  agenttalk composing --from $SELF --to $PEER -m "still drafting"
  ```
- **`agenttalk wait --grace SECONDS` (default 2.0).** Post-timeout
  grace window: when the deadline fires, sleep this long and do
  ONE final inbox scan before exiting 1. Catches replies that
  landed in the last fraction of a second before the deadline.
  `--grace 0` reproduces the pre-0.8.0 hard-edge behavior.
- **`agenttalk wait --composing-extend SECONDS` (default 120.0).**
  Per-composing-ping deadline extension, capped at 1800 s total
  per wait. `--composing-extend 0` disables extension while still
  consuming composings (they don't surface as replies).
- **`agenttalk recv --include-control`.** Surfaces control-plane
  kinds (currently just `composing`) in the inbox view. Useful for
  debugging deadline extensions or auditing what the peer pinged.
- **sk-loop skill update.** Both Claude and Codex sk-loop skills
  gained two patterns:
  - **Cycle-2+ example request:** if the reviewer's feedback is
    ambiguous, the implementer sends a `question` asking for a
    concrete sketch BEFORE guessing into another rejection. Uses
    the existing `question` kind — no new bus surface.
  - **Composing pings during long replies:** the drafting agent
    sends `agenttalk composing` every ~2 min so the peer's wait
    doesn't expire mid-reply.

### Changed

- **`Store.last_received_for(agent)` now skips `CONTROL_KINDS`** by
  default, so `agenttalk reply` auto-correlates to the most recent
  real message rather than to a stale composing ping. The kept-out
  set is configurable via a new keyword argument.
- **`agenttalk send --kind`** help text now lists `composing` as a
  known kind and points to the dedicated `composing` subcommand as
  the preferred entry point.

### Tests

- 10 new tests in `tests/test_cli.py` covering: post-timeout grace
  returns a late message; `--grace 0` preserves pre-0.8.0 behavior;
  composing extends the deadline and is not returned as a reply;
  `--composing-extend 0` disables extension but still consumes the
  ping; duplicate composings counted only once (no runaway loop);
  the `composing` subcommand writes the right kind + body; `recv`
  hides composings by default; `--include-control` surfaces them;
  `--ack` advances past a hidden composing so a stale ping can't
  pin a cursor forever.

## [0.7.2] — 2026-05-22

Patch release. UX cleanup for the upgrade path: surfaces drift
between bundled and installed skill files better and makes
`install-skills --dry-run` actually look like a dry run.

### Fixed

- **`agenttalk install-skills --dry-run` is now visibly
  different from a normal run.** Previously, a target that
  differed from the bundled version reported `skipped` in both
  real and dry-run modes, which made `--dry-run` look broken
  (same wall of `! skipped` lines as a real run). Dry runs now
  emit `would-skip` (target differs, `--force` not set, would
  not write) and `would-overwrite` (target differs, `--force`
  set, would overwrite). Each output line carries a short tail
  explaining the state, e.g.
  `? would-skip … (differs; local edits preserved)`.
- **`agenttalk install-skills --dry-run --force` previews
  `--force`** without writing — the recommended path before
  destroying local edits is now "dry-run --force first, then
  `--force`".

### Changed

- **`agenttalk doctor` skill-drift output is now actionable.**
  The existing `claude_skills` / `codex_skills` warn check now:
  - Names the differing files in the human-readable `details`
    (was just `N/total differ from bundled`), so you don't
    have to re-run `install-skills` to find out which.
  - Leads the `fix` hint with `--dry-run --force` (preview)
    before suggesting the destructive `--force` step. Previously
    jumped straight to `--force`, which destroys local edits
    without warning.
  - Carries a per-check `data` payload in `doctor --json`
    (`{"target": "...", "missing": [...], "differs": [...],
    "total": N}`) so loops and scripts can act on drift without
    re-parsing the human output.

### Added

- Three regression tests:
  - `test_dry_run_reports_would_skip_when_target_differs_no_force`
  - `test_dry_run_with_force_reports_would_overwrite_no_writes`
  - Doctor: extends the existing drift test to pin the new
    file-naming + `--dry-run --force` hint + JSON `data` payload.

## [0.7.1] — 2026-05-21

Patch release. Fixes a chronology bug in message id generation
that surfaced as a flaky test in CI on fast hardware after the
0.7.0 release.

### Fixed

- **`_new_id` is now strictly monotonic within a process.** The
  format ``YYYYMMDD-HHMMSS-uuuuuu-XXXX`` documented
  lexicographic-equals-chronological order, but the random
  4-char suffix did not preserve order when two messages landed
  in the same microsecond. ``messages_for`` and the new web
  dashboard sort by id to display chronological order, so the
  bug could surface two same-microsecond messages in send-
  reversed order (visible in ``recv``, ``tail``, and
  ``/messages``). Fix: per-process monotonic clock — if a new
  call would tie or go backwards relative to the last issued
  id, the timestamp is bumped by 1µs. Cross-process collisions
  (two agents writing the same microsecond) are still handled
  by the random suffix; each process tracks its own last id.

### Added

- ``test_new_id_is_strictly_monotonic_under_load`` (tight-loop
  regression test, 2000 calls) that pins the invariant against
  future fast-hardware regressions.

## [0.7.0] — 2026-05-21

Adds a read-only local web dashboard so the message log can be
browsed in a real browser instead of being limited to terminal
output. No write actions; no new runtime dependencies (stdlib
`http.server` + `html` + `json`).

### Added

- **`agenttalk serve` — read-only local web dashboard.** Starts
  a small HTTP server (default `http://127.0.0.1:8765/`) that
  renders the roster, signing status, message list, and per-
  message detail pages. **Loopback-only by design** — the only
  accepted `--host` values are `127.0.0.1`, `::1`, and
  `localhost`; there is no flag to expose the dashboard
  elsewhere. SSH-tunnel `localhost:<port>` if you need to view
  it from another machine. All HTML output is escaped, a strict
  `Content-Security-Policy` blocks inline JS, and only
  `GET`/`HEAD` are allowed.
- **JSON endpoints for scripting.** `/api/status` mirrors the
  same data `agenttalk status --json` reports plus `hmac_key`
  health and `invalid_messages`. `/api/messages` returns every
  validated message; `/api/messages/<id>` returns one.
- **Tests:** `tests/test_web.py` (20 tests) covering the index
  render, JSON endpoints, message detail, POST/PUT/DELETE
  rejection, path-traversal handling on `/messages/<id>`, HTML
  body escaping (defense against an LLM smuggling JS via a
  message body), refusal to bind any non-loopback host,
  per-request loopback peer check on **every** HTTP method
  (not just GET — closes a method-skip information leak), IPv6
  URL bracketing (`http://[::1]:port/`), response security
  headers, and parity-with-`recv`/`tail` checks: messages with
  unknown `kind`, out-of-roster sender/recipient, or bad HMAC
  signatures do NOT render and instead surface under
  `/api/status.invalid_messages`.

### Security

- The dashboard treats message bodies as untrusted: every body
  is escaped via `html.escape` AND the page sets
  `Content-Security-Policy: default-src 'none'; style-src
  'unsafe-inline'; img-src 'none'; frame-ancestors 'none'` so
  even if an escape were ever missed, inline JS would not run.
- The dashboard reuses `Store.messages_for`'s validation surface
  (schema, roster, kind, and HMAC when `signing_enforced()`).
  Messages that fail any of these checks do not appear in
  `/api/messages` or `/messages/<id>` — they surface under
  `/api/status.invalid_messages`, matching the
  `recv`/`tail`/`doctor` invariant.
- The per-request loopback-peer check runs before EVERY HTTP
  method. Earlier dashboard iterations let POST/PUT/DELETE/PATCH
  skip the check and return 405 without the peer gate, which let
  a non-loopback probe distinguish "server present" from "you
  are blocked." Both now collapse to 403.

## [0.6.0] — 2026-05-21

Security release. Closes the `kind=end` forgery scenario and the
zizmor follow-up from 0.5.1.

### Added

- **Optional HMAC-SHA256 message signatures.** Stdlib-only
  (`hmac` + `hashlib`). Key lives in the per-user config dir
  (`~/.config/agenttalk/keys/<project_id>.key` on POSIX,
  `%LOCALAPPDATA%\agenttalk\keys\<project_id>.key` on Windows) —
  outside `.agenttalk/` so an attacker with project-dir read
  CANNOT also read the key. **Enforcement is anchored to the
  per-user key file's existence at a PATH-DERIVED `project_id`
  (SHA-256 of the absolute project root path)**, NOT to any field
  in attacker-writable `.agenttalk/config.json`. The iter-1
  review caught that a `require_signatures` config flag was
  bypassable by editing config.json; the iter-2 review caught
  that a config-stored `project_id` was bypassable the same way.
  After iter-2, **nothing inside `.agenttalk/` is load-bearing
  for signature enforcement** — both the project identity and the
  policy live outside the project dir. To enable: `agenttalk
  hmac-init`. To disable: delete the key file. Tradeoff: moving
  the project dir changes its path-derived `project_id`, so you
  re-run `hmac-init` at the new location (documented in
  SECURITY.md). Wire format pinned by a golden test
  (`tests/test_signing.py::test_canonical_payload_format_is_stable`)
  and documented in `src/agenttalk/signing.py`. Defaults OFF
  (zero-setup path unchanged from 0.5.x).
- **`agenttalk hmac-init [--force]`** — bootstrap the project's
  signing key. Writes mode 0600 on POSIX. Activates enforcement
  on both send AND verify sides — there is no separate "enable"
  flag.
- **`agenttalk status` / `status --json`** — surfaces `project_id`
  (path-derived), `signing_enforced` (key-file presence), and —
  when enforced — `hmac_key` health (path / exists / readable /
  mode warning / in-project-dir warning). Legacy
  `require_signatures` and `project_id` fields in config.json
  from upgraded 0.6.0-iter-1/2 configs are surfaced with a NOTE
  that they're ignored (kept for diagnosability).
- **`agenttalk doctor`** — new `hmac` check, ok/warn/error with
  remediation hints (no project_id, key file inside project,
  permissive mode, unreadable, etc). Reports `disabled` when no
  key file exists (the OK default).
- **`.github/workflows/*.yml`** — every third-party action now
  hash-pinned (`actions/checkout@<sha>  # v4` etc.); `# vX`
  comment kept dependabot-friendly.
- **`.github/dependabot.yml`** — weekly auto-bumps for GHA SHAs
  and dev-dep security updates.

### Changed

- **zizmor now votes** in CI (was non-voting in 0.5.1). All
  third-party actions are hash-pinned so the `unpinned-uses`
  default policy is satisfied. Workflow-level permissions stay
  read-only; CodeQL grants itself `security-events: write` at
  job level.
- **`Store.project_id()` is path-derived.** SHA-256 of the
  absolute project root path; not stored in config. The iter-2
  review found that a UUID stored in config.json could be
  tampered with to disable enforcement; the iter-3 design moves
  project identity entirely outside `.agenttalk/`. Existing 0.5.x
  configs continue to work unsigned; enabling signatures on any
  project (new or existing) is just `agenttalk hmac-init` — no
  config edits, no flags. Tradeoff: moving the project dir
  changes its `project_id`, so the key file must be regenerated
  at the new location. Documented in SECURITY.md.
- **`agenttalk tail` now validates HMAC** before rendering
  message bodies (iter-1 review caught that tail bypassed
  signature verification and rendered forged bodies as normal
  output). Forged-signature / unsigned-when-required / missing-
  key messages now surface as `TAIL INVALID` warnings on stderr;
  the body is never rendered.
- **`Message.from_dict` vs `Message.from_raw`** distinction kept;
  signature ops live in `agenttalk.signing` so `Message` stays a
  pure data layer per Codex's design feedback.

### Security

This is the first release that actually defends against an
adversarial *other local user with project-dir write access*.
Earlier releases were honest about that being out of scope; 0.6.0
makes it opt-in. **Limits documented in SECURITY.md:** HMAC does
NOT defend against the same OS user (they can read the key
anywhere it's stored), does NOT solve replay / deletion /
reordering / cursor tampering (those need a hash chain anchored
outside `.agenttalk/`, tracked for later), and does NOT validate
timestamps for freshness (clock skew would create avoidable
failure modes).

### Tests

- 258 passing + 2 skipped (POSIX mode-bit tests skipped on
  Windows). Up from 227 in 0.5.2. Coverage includes canonical
  payload golden test, sign/verify round-trip, every failure
  mode (missing/wrong-version/wrong-key/tampered-body/wrong-key-
  id), key file I/O + mode 0600 enforcement, key health
  inspection (missing/secure/in-project-dir/loose-mode), Store
  integration (sign-on-send, verify-on-read, unsigned-rejected-
  when-key-exists, forged-signature-rejected, valid-signature-
  accepted, zero-setup backwards-compat). Two iter-2 regression
  tests are critical: the config-tamper bypass repro (config
  edit cannot disable enforcement) and the tail-bypass repro
  (tail uses the same verifier as messages_for).

### Notes for upgraders

- **Existing projects:** zero change required. Without a key file
  for this project, enforcement is off; old configs work
  unchanged. (Note: 0.5.x's path-relative install instructions
  and 0.6.0's path-derived project_id mean that moving a project
  directory changes its identity. If you upgrade in place and
  don't move the dir, nothing changes for you.)
- **To enable signatures on a fresh project:**
  ```
  agenttalk init --here --agents claude,codex
  agenttalk hmac-init
  ```
  Enforcement activates as soon as the key file exists.
- **To enable on an existing project:** just `agenttalk hmac-init`.
  No config edits, no flags. The project_id is derived from the
  absolute project root path. There is no `require_signatures`
  flag in 0.6.0 — earlier iter-1 designs had one but Codex's
  review found it was attacker-writable and therefore useless;
  the key file's existence at the path-derived ID is the policy
  anchor. (iter-2 review also caught that a config-stored
  project_id had the same bypass; both are now removed.)
- **Skill bodies are unchanged** in this release. Existing
  installs of the bundled slash commands don't need
  `agenttalk install-skills --force`.

## [0.5.2] — 2026-05-21

Tag-hygiene patch. v0.5.1 introduced ruff in CI but the
auto-fixes for six test files (removing unused imports, dropping
an unused variable) landed on disk but weren't staged into the
release commit. CI on the v0.5.1 tag would have failed ruff. This
patch commits those cleanups so the tagged tree matches the
local-clean baseline.

### Fixed

- `tests/conftest.py`, `tests/test_codex_config.py`,
  `tests/test_reply_tail.py`, `tests/test_reset.py`,
  `tests/test_skill_lint.py`, `tests/test_store.py`: dropped
  unused imports (`os`, `json`) and an unused assignment that
  ruff F401/F841 had flagged in 0.5.1 but the v0.5.1 commit
  missed staging.

No behavior changes. 227 tests still pass. Ruff `check src tests`
now reports `All checks passed!` against the *committed* tree
(matching the v0.5.1 promise).

## [0.5.1] — 2026-05-21

Tooling-only patch. No behavior changes; closes the "CI scanner
integration" track from the v0.2.0 review report.

### Added

- **GitHub Actions workflows.**
  - `.github/workflows/security.yml` — runs the full security
    scanner stack on every push to master, PR, and a weekly
    schedule: ruff (with `S` rules), bandit, pip-audit, gitleaks,
    semgrep (registry + custom local rules), CodeQL
    `security-extended`, and zizmor (GHA workflow audit).
  - `.github/workflows/tests.yml` — pytest matrix across Python
    3.10–3.13 on ubuntu/windows/macos.
- **Custom semgrep rules** at `.semgrep/agenttalk.yml` enforcing
  agenttalk-specific invariants:
  - Raw agent names must not be interpolated into state filenames
    without `validate_agent_name()`. (The class of bug fixed in
    0.2.1; semgrep flags it before it ships.)
  - Messages must go through `Store.send()`, not direct file
    writes into `.agenttalk/messages/`. (Bypasses the KNOWN_KINDS
    + atomic-write guarantees.)
  - `exec()` / `eval()` on a message body is a hard fail (the
    explicit prompt-injection threat called out in SECURITY.md).
- **Ruff configuration** in `pyproject.toml`. Line length 120;
  rules `E F B S C4` selected. Per-file ignores for tests
  (asserts + literal "passwords" used as message bodies).
- **New optional dependency group:** `[security]` installs ruff,
  bandit, and pip-audit for local pre-commit-style runs.

### Changed

- Several small style cleanups across `src/agenttalk/` and
  `tests/` to satisfy the new lint baseline. No behavior
  changes — `Message.from_raw` field loop variable renamed
  (`field` shadowed an import), exception chains use
  `raise ... from e`, and a few long lines were wrapped.

### Tests

- Still 227 passing.
- Bandit run produces 0 issues at all severity levels.
- Ruff `check src tests` produces 0 errors.
- pip-audit runs against `pip freeze --exclude-editable` from
  a fresh `pip install -e ".[dev]"` env. First CI run will be
  the ground truth for the dev-dep CVE set.

### Notes

- Scanners run in CI only. There are no new runtime dependencies;
  the agenttalk package remains stdlib-only.
- `SECURITY.md` updated: "CI scanner integration" moved from
  "Still planned" into "Delivered in 0.5.1".

## [0.5.0] — 2026-05-21

Third slice of the v0.2.0-review roadmap. Adds two ergonomic
commands that close the gap between the existing primitives.

### Added

- **`agenttalk reply`** — reply to the most recent received message.
  Auto-derives the recipient (= sender of the last message) and
  auto-echoes `request_id` from the original meta so the peer's
  `wait`-then-match logic works without the agent manually
  threading the correlation token. Explicit `--meta request_id=...`
  wins over auto-echo. Kind defaults to `message` (the safe
  no-op default) rather than auto-promoting to `review-result`.
  Resolves the v0.2.0-review "agenttalk reply" item.
- **`agenttalk tail`** — passive monitor mode. Streams every
  message as it arrives, using the same display renderer as
  `wait`/`recv`, **without** advancing any cursor or writing any
  heartbeat. Safe to run in a third terminal alongside two active
  agents — they don't see tail as a listener. `--from-start`
  replays the entire store first; `--timeout N` exits after N
  seconds (default 0 = run until Ctrl-C). Resolves the v0.2.0-
  review "agenttalk tail/watch" item.
- **`Store.last_received_for(agent) -> Message | None`** — the
  underlying primitive for `reply`. Honors schema/roster
  validation: a tampered message that `messages_for()` would
  filter is never returned as "the last message".

### Tests

- 227 passing (was 208 in 0.4.0). New file `tests/test_reply_tail.py`
  covers: reply auto-derives recipient, auto-echoes request_id,
  explicit meta wins, empty inbox exits 2, kind defaults to
  message, explicit kind wins, env-driven self, unknown kind
  rejected; tail streams only new by default, --from-start
  replays, never advances cursors, never writes heartbeats, exits
  0 on timeout, picks up messages injected during the run, NEVER
  renders forged/unknown-kind message bodies (surfaces them as
  INVALID warnings on stderr), surfaces unparseable-JSON warnings
  too; last_received_for returns most recent, returns None on
  empty, skips invalid messages.

## [0.4.0] — 2026-05-21

Second slice of the v0.2.0-review roadmap. Adds explicit session
lifecycle (`agenttalk reset` / `reset --archive`) and clarifies
what `init --force` actually does.

### Added

- **`agenttalk reset [--archive]`** — clears **active bus state**
  (messages, cursors, heartbeats) and bumps `session_id`.
  **Preserves the config (roster) AND historical transcripts under
  `.agenttalk/sessions/`** — exported transcripts are user-visible
  artifacts, not active bus state. With `--archive`, instead moves
  everything (messages + state + sessions) into
  `.agenttalk/archived/<old_session_id>/` so the entire prior
  session is recoverable. Closes the v0.2.0 review's "no clean
  lifecycle around old messages" gap. Session IDs are now
  validated as safe filesystem-path fragments at config-load time,
  so a corrupt config can't smuggle a `..\\escaped` path through
  to `--archive`.
- **`Store.reset(archive=False) -> (cfg, archive_path | None)`**
  and **`Store._archive_session(session_id) -> Path`** as the
  underlying mechanism. Same-filesystem `shutil.move` so even
  large message dirs archive instantly. Collision-safe: a second
  archive into the same `session_id` writes timestamped sub-dirs
  rather than overwriting.

### Changed

- **`agenttalk init --force` semantics clarified.** Previously
  ambiguous about whether `--force` cleared in-flight messages.
  The help text now spells out: it rewrites `config.json` only;
  state (`messages/`, `state/`, `sessions/`) is preserved. Users
  who want a clean slate are pointed at `agenttalk reset`.
- **`Store._new_session_id()` now includes a 4-char random
  suffix.** The old `YYYYMMDDTHHMMSSZ` format collided when two
  session boundaries fell in the same second (init then reset).
  New format: `YYYYMMDDTHHMMSS-XXXXZ`. Filename-safe; transcript
  paths stay unique.

### Tests

- 208 passing (was 184 in 0.3.0). New file `tests/test_reset.py`
  covers: state clearance, session-id rotation, archive
  preservation (including transcripts under `--archive`),
  transcript preservation by default, double-archive collision
  safety, empty cursor recreation, uninitialized-store error, CLI
  default vs `--archive`, exit-2 on uninitialized, the explicit
  `init --force does NOT clear` guarantee, the `init --help` text
  mentions `agenttalk reset`, the session-id traversal regression
  (corrupt config rejected at load time), parametrized rejection
  of 7 unsafe session-id forms, and parametrized acceptance of
  both old (`YYYYMMDDTHHMMSSZ`) and new (`YYYYMMDDTHHMMSS-XXXXZ`)
  formats for backwards-compat.

## [0.3.0] — 2026-05-21

First slice of the v0.2.0-review feature wave. Adds two new
commands, product-level message-schema hardening, structured-output
support for automation, and a substantial test-coverage backfill.

### Added

- **`agenttalk status --json`** — structured output for the consult
  freshness check and any external automation. Schema:
  `{root, session_id, message_count, invalid_messages[], agents[],
  stale_threshold_seconds}`; each agent entry exposes `cursor`,
  `unread`, `heartbeat` (ISO 8601 or null), `last_seen_seconds`,
  and `stale` (bool or null).
- **`agenttalk doctor`** — single-command health check. Reports:
  is the store initialized, are bundled skills installed and in
  sync with the bundled package, is the per-project Codex sandbox
  block configured, and is each agent's heartbeat fresh. Each check
  carries `ok / warn / error` plus a one-line remediation hint.
  Supports `--json` for the same payload. Per the global exit-code
  contract, exit 2 on any error; warnings exit 0 with the warning
  state visible in output. (Exit 1 stays reserved for `agenttalk
  wait` timeout.)
- **Message-schema validation on read.** New `KNOWN_KINDS`
  vocabulary; `Message.from_raw(data)` strictly validates raw JSON
  before constructing a Message (catches missing/wrong-type id, ts,
  from, to, kind, subject, body, meta — including the `meta=[]`
  truthy-coercion bug and non-dict roots); `Message.validate(roster)`
  enforces known kind + roster membership. `Store.messages_for()`
  silently skips invalid messages so a forged file with an unknown
  kind cannot smuggle a fresh instruction surface into the listener.
  `Store.list_invalid_messages()` returns every parse failure
  alongside every schema/roster failure so tampering and disk
  corruption are visible rather than silently swallowed; `agenttalk
  status` shows the count and points users at `status --json` for
  per-message details.
- **Send-time kind validation.** `Store.send()` rejects unknown
  kinds at write time — without this, a `--kind typo` would exit 0
  with the message silently undeliverable on the receive side.
- **Skill-body untrusted-input guidance.** listen, sk-loop, and
  consult skill bodies (both sides) now carry explicit prompt-
  injection-resistance rules: message bodies are data the LLM is
  asked to read, not instructions the LLM is asked to follow;
  state transitions must derive from validated metadata + repo
  reading, never from body prose alone.
- **Consult skill uses `status --json`** for the heartbeat
  freshness check instead of parsing the human-formatted output —
  the JSON contract is the stable one.

### Tests

- Suite grew from 93 to 184. New coverage: `status --json` schema,
  `doctor` for every check category (uninitialized / fresh / stale
  / missing heartbeat / out-of-sync skills / missing codex-config
  block), message-schema validation (each rejection reason
  individually + the read-time silent-skip), `transcript.py`
  markdown + jsonl + unicode + export round-trip, `display.py`
  rendering with kind/subject/meta/empty-body/multiline/unicode
  cases, `cmd_end` (sends `kind=end` + writes transcript),
  `cmd_transcript` (md + jsonl), `cmd_wait` (already-queued
  message, timeout, heartbeat write, --ack vs --no-ack cursor
  behavior), `agenttalk --version`. Also a new skill-body lint
  asserting every required policy substring appears in both the
  Claude- and Codex-side skill bodies, catching cross-side drift.

### Changed

- `Store.messages_for()` now silently skips messages that fail
  schema/roster validation. `Store.all_messages()` returns only
  messages that survived strict raw-JSON construction; parse
  failures and construction failures are surfaced separately via
  `Store.list_invalid_messages()` (consumed by `agenttalk status`
  and `agenttalk status --json`).
- README + SECURITY.md updated to mention the new commands and
  the schema-validation behavior. README's `--kind` documentation
  also updated: kinds are now a fixed vocabulary (KNOWN_KINDS), not
  free-form; adding a new kind requires updating that constant.

### Security

This is a *data-integrity* hardening pass, not a cryptographic
one. Schema validation catches malformed or out-of-vocabulary
messages, but cannot defend against an attacker who writes
well-formed messages — that requires signing and is tracked for
0.4.0+ as opt-in HMAC (see `SECURITY.md`). The skill-body
prompt-injection guidance is the procedural counterpart and
applies regardless of whether crypto is later added.

## [0.2.1] — 2026-05-21

Patch release driven by the v0.2.0 cross-agent code review (see
[`docs/reviews/v0.2.0.md`](docs/reviews/v0.2.0.md)). Closes one
security blocker plus three quality issues that landed in the same
review pass.

### Security

- **Agent names are now validated against a safe-identifier
  pattern** (`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`). Before, names
  were interpolated directly into filesystem paths like
  `.agenttalk/state/<name>.cursor`, so a name containing `..`,
  `/`, or `\` could escape `.agenttalk/state/` and write
  arbitrary files inside the project root. `agenttalk init` and
  every identity-resolution path (`--from`/`--to`/`--for` flags,
  env vars, on-disk config) now reject unsafe names with exit 2.
  Duplicate names in the roster are also rejected.

### Fixed

- **`--meta bad_value_without_equals` now exits 2** instead of 1.
  The old `SystemExit(str)` form exited 1, which collides with
  `agenttalk wait`'s timeout signal and could confuse loop skills.
- **`agenttalk codex-config` writes are now atomic.** The
  user-global `~/.codex/config.toml` was being written with a
  plain `Path.write_text()` — a crash mid-write could truncate
  a file that controls every Codex project. Now uses the same
  temp-file + `os.replace` helper as the message store.
- **`agenttalk codex-config` now handles project paths containing
  apostrophes.** A path like `D:\Code\Bob's Repo` previously
  emitted invalid TOML (`[projects.'...Bob's Repo']`). The
  section-key renderer now uses a TOML basic string with proper
  escaping when the path contains a single quote.

### Changed

- `agenttalk._atomic.write_text` factored out of `store.py` as a
  shared helper; `codex_config.py` now uses it.
- `Store.load_config()` validates the on-disk roster on read, so a
  malformed `config.json` (wrong types, unsafe names) errors out
  with a clear message instead of cryptic downstream tracebacks.
- README: agent-identity section now documents the safe-identifier
  rule; CLI reference gained an Exit Codes table.

### Tests

- New tests cover: the safe-identifier pattern (parametrized over
  valid + invalid names, including trailing-newline cases that the
  Python `$` anchor would have missed), exact- and case-insensitive
  duplicate-roster rejection, load-time corrupt-config detection,
  the path-traversal regression (Codex's review repro), invalid-
  `--meta` exit code, apostrophe-path TOML emission, idempotent
  round-trip through the new TOML quote helper, and the
  codex_config atomic-write contract across all three call sites.
- Total suite: 93 tests, all passing.

### Docs

- `SECURITY.md` added: trust model, threat scenarios with expected
  behavior under the current model, planned 0.3.x product-level
  hardening, recommended CI scanner stack, and a clear statement of
  what `agenttalk` does NOT defend against (same-OS-user attacker,
  shared filesystems, prompt injection in message bodies).
  Synthesizes the cross-agent security consult done via
  `/agenttalk.consult` on the same day.
- `docs/reviews/v0.2.0.md` added: the v0.2.0 review report that
  drove this patch.

## [0.2.0] — 2026-05-21

First tagged release. Builds out the message bus into a full
cross-agent collaboration surface, with bundled slash commands for
Claude Code and Codex, a persistent spec-kitty loop, and a
pre-answer consult primitive.

### Added

- **`agenttalk codex-config`** — manages per-project sandbox/trust
  block in `~/.codex/config.toml` so Codex can call agenttalk from
  inside its sandbox. Idempotent enable/disable/status with TOML
  parsing that handles single- and double-quoted Windows-path keys
  and inline-comment table headers.
- **`agenttalk install-skills`** — copies bundled skill files to
  `~/.claude/commands/` and `~/.codex/skills/`. Idempotent,
  preserves user edits unless `--force`; supports `--dry-run`,
  `--claude-only`, `--codex-only`.
- **Bundled skill set** (5 per side, installed by
  `agenttalk install-skills`):
  - `/agenttalk.send` — fire-and-forget message.
  - `/agenttalk.listen` — passive listener with reentrant wait,
    mode-detecting `review-request` handling (spec-kitty vs ad-hoc
    cross-review), and consult-request critique routing.
  - `/agenttalk.handoff` — bundled send + wait with structured body
    template and `request_id` correlation.
  - `/agenttalk.sk-loop` — persistent spec-kitty implement/review
    loop driven by `spec-kitty next` state machine, with `kind=wake`
    signals between two persistent CLI windows. Role-symmetric.
  - `/agenttalk.consult` — confer with the peer *before* answering
    the user. Sends a structured draft + uncertainty, waits for
    critique, synthesizes a concise final answer naming agreement
    and disagreement.
  - Codex side mirrors all five under `~/.codex/skills/agenttalk-*`.
- **Heartbeats** — `agenttalk wait` stamps
  `.agenttalk/state/<agent>.heartbeat` every 10 s (configurable via
  `--heartbeat-interval`); `agenttalk status` shows `last_seen=<age>`
  per agent so peers can see whether the other side is actively
  listening. Pure observability, no LLM tokens.
- **`agenttalk --version`** — prints the installed version (useful
  for support).
- **Pytest suite** — 46 tests across store, codex_config,
  install_skills, and cli, including regressions for every
  Codex-caught bug from earlier review rounds.

### Changed

- **Env-driven agent identity.** `--from`/`--to`/`--for` are now
  optional on every CLI command and fall back to `$AGENTTALK_SELF`
  / `$AGENTTALK_PEER`. In a 2-agent roster, the peer can be derived
  automatically. Required to support running two agents of the same
  kind (e.g., two Claudes) with distinct names.
- **Roster validation.** Resolved identities must be in the project
  roster — typos exit 2 with a clear message instead of silently
  operating on a phantom mailbox. Self-mail (`--from X --to X`) is
  rejected.
- **Listen wait timeout 30 s → 1800 s.** Pure listen mode has
  nothing else to interleave with; short timeouts were burning tokens
  on idle wake-ups. sk-loop keeps its 30 s for the spec-kitty poll
  interleave.
- **sk-loop made role-symmetric.** Both `implement` and `review`
  actions are equally normal for either agent; spec-kitty's state
  machine assigns roles per WP.
- **No-auto-split rule** baked into listen, handoff, and send skill
  bodies: outside spec-kitty, agents must not coordinate a split of
  implementation work without first asking the user. When the user
  approves a split, every implemented piece must be cross-reviewed.
- **`agenttalk init` output** now prints concrete env-setup commands
  for both terminals when the roster has exactly two agents.

### Fixed

- Heartbeat parser previously returned a naive datetime for
  timezone-less heartbeat files, crashing `status` via
  aware-vs-naive subtraction. Now returns `None` on naive input —
  status degrades to `(no heartbeat)` instead of crashing.
- `codex-config` parser previously failed to match TOML basic-string
  keys with escaped backslashes (e.g.
  `[projects."d:\\Projects\\repo"]`) and headers with inline comments
  (e.g. `[projects.'...'] # comment`), silently creating duplicate
  blocks instead of being idempotent.
- `cli.main()` exit-code propagation: `python -m agenttalk` now
  exits with the correct status code (was always 0).
- Windows stdout/stderr reconfigured to UTF-8 in `cli.main()` —
  arrow and em-dash characters in message bodies no longer crash via
  the cp1252 codec.
- README install path was ambiguous (`pip install -e .\agenttalk`
  required the wrong CWD). Now standardized on `cd agenttalk &&
  pip install -e .`.
- `OSError` / `PermissionError` is now caught in the top-level
  exception handler — permission failures give a clean CLI error
  instead of a traceback.

### Notes

- Package remains stdlib-only (no third-party runtime dependencies).
- Python 3.10+ required.
- Not on PyPI by design. Install via tag-pinned git URL (see README).

## [0.1.0] — initial commit

- Core file-backed message bus (`agenttalk init`/`send`/`recv`/
  `wait`/`ack`/`transcript`/`end`/`status`).
- Atomic JSON-per-message writes; per-agent cursors; markdown +
  JSONL transcript export.

[0.12.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.12.0
[0.11.1]: https://github.com/zoolok17/agenttalk/releases/tag/v0.11.1
[0.11.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.11.0
[0.10.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.10.0
[0.9.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.9.0
[0.8.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.8.0
[0.7.2]: https://github.com/zoolok17/agenttalk/releases/tag/v0.7.2
[0.7.1]: https://github.com/zoolok17/agenttalk/releases/tag/v0.7.1
[0.7.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.7.0
[0.6.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.6.0
[0.5.2]: https://github.com/zoolok17/agenttalk/releases/tag/v0.5.2
[0.5.1]: https://github.com/zoolok17/agenttalk/releases/tag/v0.5.1
[0.5.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.5.0
[0.4.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.4.0
[0.3.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.3.0
[0.2.1]: https://github.com/zoolok17/agenttalk/releases/tag/v0.2.1
[0.2.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.2.0
