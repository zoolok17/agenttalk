# Changelog

All notable changes to agenttalk are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.5.2]: https://github.com/zoolok17/agenttalk/releases/tag/v0.5.2
[0.5.1]: https://github.com/zoolok17/agenttalk/releases/tag/v0.5.1
[0.5.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.5.0
[0.4.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.4.0
[0.3.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.3.0
[0.2.1]: https://github.com/zoolok17/agenttalk/releases/tag/v0.2.1
[0.2.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.2.0
