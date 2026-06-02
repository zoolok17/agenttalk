# Changelog

All notable changes to agenttalk are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.6.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.6.0
[0.5.2]: https://github.com/zoolok17/agenttalk/releases/tag/v0.5.2
[0.5.1]: https://github.com/zoolok17/agenttalk/releases/tag/v0.5.1
[0.5.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.5.0
[0.4.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.4.0
[0.3.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.3.0
[0.2.1]: https://github.com/zoolok17/agenttalk/releases/tag/v0.2.1
[0.2.0]: https://github.com/zoolok17/agenttalk/releases/tag/v0.2.0
