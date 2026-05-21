# Changelog

All notable changes to agenttalk are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.2.0
