# Changelog

All notable changes to agenttalk are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.1]: https://github.com/zoolok17/agenttalk/releases/tag/v0.2.1
[0.2.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.2.0
