# agenttalk — roadmap & working notes

Carry-forward notes for the next session / next machine: what production
use validated, what hurt, and the ranked backlog to address it. Built from
the Claude Code + Codex collaboration that shipped 0.10.0–0.13.0, the first
four-agent production retro (2026-06-03), **and the band's consolidated
second retro (2026-06-05), which reprioritized everything below.**

> Current release: **v0.24.0** (`master`). See `CHANGELOG.md` for history
> and `SECURITY.md` for the trust model.

---

## Phased plan (reprioritized 2026-06-05)

Sequenced jointly by Claude + Codex against the band's evidence. The
serial model stands: **Claude implements (Python/tests), Codex reviews +
docs, fresh-eyes reviewers spawned on demand** — production explicitly
confirmed more standing agents are not worth the coordination overhead.

### ✅ Phase 1 — `0.13.0` "workflow safety + Windows robustness" — DELIVERED 2026-06-03
Shipped #6 (`reply --dry-run`), #7 (`--file -` stdin bodies), #8
(`whoami`), #10 (skill-doc / identity-bootstrap fixes). All four issues
closed. Kept here as a record only.

### ✅ Phase 2 — `0.14.0` "operator safety" — DELIVERED 2026-06-05
Shipped #12 (rescind + check gate), #13 (root hardening), #18 (operator
liaison), AND #14 (intent-to-reply — the slip clause was not needed). All
four issues closed. Built as spec-kitty mission `operator-safety-0140`
(5 WPs, Codex per-WP review + fresh-eyes pre-release review). Record:
1. **Rescind/supersede + currentness check** (#12) — first-class
   `rescind` kind (KNOWN_KINDS, *not* CONTROL_KINDS), `closed-superseded`
   thread state, scoped-wait "request rescinded" wake, `sync`/`threads`
   flags, and `agenttalk check --to-request RID` → current | superseded |
   stale as the executable pre-action barrier. Generic primitive only —
   HOLD/VOID conventions stay in skills/governance, per the band's own
   unanimous line. Global epochs/send-time barriers explicitly deferred
   to the RFC (#19).
2. **Root hardening** (#13) — nested-`init` guard (the real split-brain
   mechanism is two `init`s + the upward root walk, *not* silent store
   creation — verified), `doctor` multi-store detection, `AGENTTALK_ROOT`
   env var, resolved root printed first in `whoami`/`doctor`.
3. **Operator liaison** (#18, promoted 2026-06-05 on operator demand) —
   `roster set-operator-facing <agent>` (advisory, independent of
   `role=lead`; recommended setup: lead == the exactly-one liaison),
   zero/multiple WARNs in `doctor`/`sync`, and an explicit
   **`agenttalk escalate`** helper: resolves the liaison, forces the
   `request_id`, **refuses (exit 2) on an ambiguous liaison set** unless
   `--to` overrides. No new kind — existing kinds + `needs_operator=true`
   meta; closure = any non-control correlated liaison reply to the
   requester. Liaison gets an "operator-input needed" bucket in
   `sync`/`threads`; no new threadstate. Lead skill becomes the
   operator's single voice; worker skills escalate instead of asking
   their own window's human.
4. **Intent-to-reply sugar** (#14) — `composing --to-request RID` +
   reply-in-flight visibility in `threads`/`sync` + stale-warning
   suppression. Ships in 0.14.0 **only if it stays observational and
   small** (the mechanics are ~80% built: scoped wait already honors
   request_id-tagged composing). **First slip candidate** if the
   release gets heavy now that #18 is promoted in.

### ✅ Phase 2b — `0.15.0` "team scope" — DELIVERED 2026-06-05
Shipped #15 (role audiences + reply --na), #16 (delivery accounting
+ broadcast --resume), #17 (recoverable quarantine). All three issues
closed. Mission `team-scope-0150` (5 WPs, 7 Codex review rounds,
fresh-eyes APPROVE + notes fixed, CI matrix green BEFORE tagging).
Record:
5. **Role-scoped audiences + not-applicable replies** (#15) —
   `broadcast --to-role` with the audience **frozen into fan-out meta at
   send time** (never live-plumb roles into derivation; history must not
   drift), plus `reply --na` (structured meta on a normal message — it
   already closes the obligation today; a first-class kind only if that
   proves too weak).
6. **Broadcast preflight + manifest + partial-failure surfacing** (#16) —
   same release as role audiences (they expand fan-out usage).
7. **Invalid-message quarantine** (#17) — `prune --invalid` →
   `.agenttalk/quarantine/`; recoverable, never hard-delete; verified
   safe by construction (pure-function derivation, id-string cursors,
   per-message HMAC).

### Phase 3 — "trust": per-agent identity / authz RFC (#19)
RFC committed (`docs/rfc-identity-authz.md`); **Phase A delivered as
`0.16.0`** (see below). Phases B/C/D remain: B is the operator-gated
stdlib-crypto fork (stay stdlib-only / external signer / relax), C is
real authz, D is replay/deletion hardening.

#### ✅ Phase 3a (RFC Phase A) — `0.16.0` "trusted-team safety" — DELIVERED 2026-06-05
Mission `trusted-team-safety-0160` (spec-kitty, 4 WPs, single serial
lane; Claude implements, Codex per-WP review, CI matrix gate). Shipped:
- **identity registry** in `config.json` with permanent non-rebindable
  tombstones; history validated against the KNOWN roster (active ∪
  retired) so a retired identity's past messages stay valid;
- **`roster retire` / `rename --drain-check` / `remove [--force]` /
  `forward --to-request`** — #9 safe-rename folds in here (retirement,
  not rewrite); `remove` refuses by default with a retire hint;
- **global epochs / send-time barriers** — `barrier bump`, epoch id =
  the barrier message id, auto `epoch_at_send` (three-state), `check
  --epoch` (fails closed on the exit code, fails open vs suppression —
  documented);
- **tool-visible `next_owner` / `next_action`** on open threads (the
  soft-deadlock follow-up) — a pure read-only state projection;
- **honest docs** — SECURITY.md states trusted-team-not-authz,
  fail-open-vs-suppression, registry-no-more-trustworthy-than-roster.

Still in later RFC phases (NOT in 0.16.0): per-agent crypto, policy
permissions, hash-chain replay defense, enforceable `operator_facing`.

### ✅ Phase 4 — `0.17.0` "obligation dashboard" — DELIVERED 2026-06-07
Issue #20 (design converged over the bus: Claude proposal → Codex
counter — extend `serve`, don't fork it — → accepted). Mission
`obligation-dashboard-0170` (3 WPs, single serial lane, Codex pre-code
design review + per-WP review; WP01/WP02 approved with ZERO findings).
Shipped:
- **`agenttalk dashboard`** — multi-root read-only obligation view on
  the existing loopback-only server: roster hierarchy (liaison first),
  presence/unread/composing, open threads with `next_owner` →
  `next_action`, mission/WP tags, epoch staleness; ~2 s auto-refresh;
  repeatable `--store`, degraded-root error isolation;
- **`GET /api/state`** (`schema_version: 1`) for automation — subjects
  and derived fields only, never bodies;
- **FR-010 bind-failure honesty** — `serve`/`dashboard` exit 2 with a
  `--port 0` hint (born from a live `WinError 10013` operator report);
- per-route CSP split + full-tree-hash no-mutation regression.

### Demoted / deferred
- **#9 safe rename** — ✅ DELIVERED in 0.16.0 as `roster rename
  --drain-check` / `retire` (retirement-not-rewrite, non-rebindable
  tombstones, history preserved). Closes the original ask.
- **#11 reply-all** — deferred; role-scoped audiences + `reply --na` may
  dissolve most of the need. Revisit with production evidence after #15.
  Preserved design notes: participant set = opener + original recipients
  − self; complete `reply --dry-run --all` preflight; v1 restricted to
  non-question follow-ups.

### Deferred from the v0.19.0 fresh review (post-`0.22.0` backlog)
The combined Claude-fleet + Codex fresh-eyes review (2026-06-08) shipped all
HIGH/MED findings as `0.20.0`/`0.21.0` and the LOW/NIT sweep as `0.22.0`.
These remaining items were consciously deferred (low value or accepted within
the local trust model), not dropped:
- **`renamed_to` thread aliasing** — after a mid-flight `roster rename`,
  in-flight pairwise threads opened to/from the old name aren't matched to the
  successor (the old name is a tombstone you can't act as). `--drain-check`
  surfaces open threads before rename, so this is an opt-in operator tradeoff.
  Real fix: pass an old↔new alias map into `derive_threads`.
- **init/reset config-lock** — `Store.init()` / `Store.reset()` write
  `config.json` outside `_config_lock()`. init is a first-write and reset is
  rare, so the lost-update window the 0.21.0 lock closes doesn't really apply;
  wrap them only if reset/init concurrency becomes a real scenario.
- **empty/single-agent roster contract** — `cmd_init` already guards ≥2 at the
  CLI; the store-level permissiveness is unpinned. Decide + pin if it matters.
- **future-id / non-wall-clock cursor** — a correctly-named future-dated id can
  still advance the monotonic cursor (SECURITY.md limitation #10). Needs a
  cursor-design decision, not an ad-hoc quarantine.
- **`multi_store` pinned-root walk** — the split-brain check walks from CWD; a
  pinned `--root` elsewhere is surfaced via the existing pinned-note rather than
  a second walk. Deepen only if it proves confusing in practice.
- **`serve` localhost display** — `serve --host localhost` prints
  `http://localhost:…` though it now binds the `127.0.0.1` literal. Cosmetic.
- **HTTPResponse teardown warning** — an intermittent benign urllib test-client
  unraisable at GC; not a runtime issue. Tidy `test_web` response closing if it
  becomes noisy.

---

## Where things stand

- **0.25.0 (2026-06-09) "budget-aware coordination"** — `agenttalk capacity
  refresh|show`: each agent self-publishes a privacy-safe, advisory snapshot of
  its own 5-hour + weekly rate-limit budget (Claude via the status-line dump,
  Codex via `~/.codex/sessions` rollouts), and the bundled lead skills factor it
  into planning — steer long/uncertain work off a near-cap agent, defer near a
  reset, warn when all owners are low; **never gates**. Built as a spike with
  Codex; strictly advisory (% + reset, not exact tokens; plan-specific; degrades
  to `unknown`). Idea + design from production: a lead should plan around limits.
- **0.24.0 (2026-06-08) "coordination polish"** acted on production feedback
  from a 4-agent mission (`agenttalk-improvements.md`): `escalate` now falls back
  to the team lead (backed by an at-most-one-`lead` roster invariant and a
  `doctor` no-target check), `wake` carries a `wk-` correlation id, and `send`
  warns before talking over an open decision you owe a peer. Claim-as-lock and
  the rest of the feedback's section 1/2 were scoped to spec-kitty, not the bus.
- **0.20.0–0.23.0 (2026-06-08) closed the v0.19.0 fresh-review program.**
  0.20–0.22 fixed all HIGH/MED/LOW review findings (review-driven, two-lane,
  fully cross-reviewed). **0.23.0 bundled the dev-discipline `devkit` skill
  pack** — `craft-code`, `test-coverage`, `review-code`, `write-docs`,
  `review-docs` — into the package as a non-spec-kitty fallback: `install-skills`
  installs it by default to `~/.claude/skills` + `~/.codex/skills` (`--no-devkit`
  / `--devkit-only` to control), and `doctor` reports its freshness.
- **0.13.0 shipped the whole Phase-1 ergonomics wave** (#6/#7/#8/#10
  closed) the same day the band's first retro landed — much of their
  friction list is a **version-skew report**: the quoting tax and part of
  the --root pain are fixed by upgrading the fleet to v0.13.0 and
  re-running `install-skills`. That upgrade is the standing first
  remediation before any new code.
- **The second retro (2026-06-05) reset priorities**: supersession and
  root hardening jumped to the top; rename and reply-all dropped off the
  production wishlist entirely.
- The four-agent topology itself keeps holding: verdict cadence,
  transcript-as-provenance (a VOID + ratified replacement handled purely
  from the record), and measured cross-AI complementarity (Codex-side
  catches skew schema/mechanics, Claude-side skew statistical/governance;
  five real catches in one cycle, two money-path).

---

## Production signal — second retro (2026-06-05, all four agents)

### Consolidated wishlist (their priority order)
1. **Supersession/barriers** — a launch HOLD and the fire message
   crossed; voided run. Four crossings in one day. → #12.
2. **Role-scoped audiences + not-applicable replies** — placeholder acks
   on reviewer-only threads; ack avoided out of fear. → #15.
3. **Enforced operator_facing** — single-voice liaison decayed across
   restarts. → #18 (advisory + escalate helper; **promoted to 0.14.0**
   2026-06-05 when the operator demanded it directly; enforcement
   question → #19).
4. **Intent-to-reply markers** — reduce crossing/duplicate sends. → #14.
5. **Turn-free wait timeouts** — idle wait/poll loops burn turns/context
   across four windows. → mitigations only (see "honest scope" below).
6. **--body-file** — already shipped (0.13.0 `--file -`); upgrade.
7. **Persistent root/identity config** — the --root "silent fork". → #13.

Plus: 562 INVALID messages persisted forever (→ #17); soft-deadlock
detected but unresolvable by the tool (→ next-action/owner exploration
in #19).

### Corrections established against the code (keep these straight)
- **No command auto-creates a store** (loud exit 2); the fork mechanism
  is two `init`s + the upward `find_root()` walk routing two windows to
  two *valid* stores. Fix at `init`/`doctor`/env, not at send time.
- **`ack --to-request` masks only the threads/sync view** — delivery and
  unread are untouched. Permanent closure is real; "masks later traffic"
  is not.
- **Any non-control reply already closes a broadcast member's
  obligation** — the band's placeholder acks were the supported pattern,
  not a workaround.
- **Intent-to-reply is ~80% built** — scoped wait already extends on
  composing pings carrying the thread's request_id.

### Honest scope (stated to the band, keep stating it)
- **Turn-free waits:** the turn cost is LLM-harness economics; the bus
  cuts the *number* of wakeups (scoped waits, intent-to-reply, composing
  extensions up to the 30-min cap) but cannot make a timed-out tool call
  free.
- **Governance rituals stay conventions** (HOLD/strike, pre-registration,
  four-eyes): unanimous band position, ours too. The transport ships
  generic primitives that conventions map onto.

---

## Production signal — first 4-agent run (2026-06-03)

### What held up (consensus across the agents)
- **Roster + groups** (`@developers` / `@reviewers` / `@all`) with
  role-suffixed identities made the 4-agent topology legible.
- **Broadcast with one shared `request_id`** gave convergence discussions
  a common thread even when replies landed in different role inboxes.
- **`threads --for` was the single strongest primitive** — it caught
  stale `owed-inbound` items after restarts and an agent's own
  thread-closure mistake.
- **Persisted store + `request_id` continuity** let everyone recover
  after crash/compaction without losing the chain.
- **The `propose` flow** handled a `codex` → `codex-rev` role switch.
- The "**message bodies are untrusted — derive state from repo +
  operator, not prose**" rule stopped the lead from acting on a stale
  `HOLD` asserted by a restart-lagged agent. Keep that rule load-bearing.

### Top friction → resolution status
1. `wait` wakes on ANY message → **fixed 0.12.0** (scoped wait).
2. Reply-target ambiguity → **fixed 0.13.0** (`reply --dry-run` + docs).
3. Closure semantics → **fixed 0.12.0** (broadened question closure,
   `ack --to-request`).
4. Restart leaves agents behind → **fixed 0.12.0** (`sync` digest).
5. Identity rename was rough → **#9, design in RFC (#19)**.
6. Windows ergonomics → **fixed 0.13.0** (`--file -`, docs); remaining
   root pain → **#13**.

### Meta-theme
Multi-agent runs are fragile around restarts and shared state. Scoped
wait + sync + the separate-cwd-per-window convention addressed most of
it; the 2026-06-05 retro narrowed the residue to supersession (#12) and
root hardening (#13).

---

## Larger design boundary (longer-term): per-agent identity / authz

Roles and groups are **routing metadata, not a trust boundary** (see
`SECURITY.md`). Optional HMAC is **project-key based**, so `from=<agent>`
is a by-convention identity among trusted local participants, not a
cryptographic one. **This is the gate to cross before extending agenttalk
beyond a fully-trusted local team.** The RFC (#19) is the vehicle; scope
above. Not urgent for the current trusted-team use; the operator-safety
backlog is what production is actually asking for.

---

## Operational notes (lessons from build + production)

### Fleet upgrade discipline (new, 2026-06-05)
Production friction reports must be read against the version the fleet
actually runs. The band's quoting-tax and bootstrap pain reproduced a
pre-0.13.0 surface a day after 0.13.0 shipped. Before acting on field
feedback: `agenttalk --version` on every window, upgrade, re-run
`install-skills`, re-test, then triage what remains.

### Separate cwd per window (shared-state fix)
Launch each agent's CLI window **in its own working directory** and keep
agenttalk pointed at the pinned `--root`. Sharing one cwd/project dir
between two Claude windows caused prompt-mirroring. Coordination is
unaffected — the bus is addressed by `--root`, not cwd.

### Windows invocation
- Prefer explicit `--from`/`--for` over relying on `AGENTTALK_SELF` (env
  doesn't persist across separate tool-call shells).
- `--root <path>` must come **before** the subcommand.
- Pipe **here-strings to `--file -`** for message bodies (0.13.0+);
  carry paths/roots/request ids in `--meta key=value`, not prose.

### Fresh-review ritual asymmetry
Spawning a fresh, context-free sub-agent to review works on both sides
(Claude via the `Agent` tool; Codex via `spawn_agent`) and earned its
keep. **But environments differ:** the Claude-side fresh agent could run
the test suite; the Codex-side one had **no Python in its sandbox**
(diff/static review only). Lean on the Claude-side reviewer for anything
that must *execute*.

### CI gate (don't repeat the 0.14.0 red-matrix miss)
The repo runs a `tests` workflow on every push: pytest on a 3.10-3.13 ×
ubuntu/macos/windows matrix (plus a `security` workflow). Local green is
NOT the gate — local runs are one Python on one OS with a configured
host (installed skills, codex config). After EVERY push:
`gh run list --limit 2`, and before tagging a release:
`gh run watch <id> --exit-status` until the matrix is green. Known trap:
`doctor`'s exit code reflects host-environment health — tests must pin
the environment (monkeypatch the skill dirs), never assert exit 0 on an
unpinned host.

### Release ritual (don't repeat the tags-vs-Releases miss)
`git push --tags` creates **tags**; the GitHub **Releases page** shows
**Release objects**, which are separate. Full ritual:
```bash
# 1. bump version (pyproject.toml + src/agenttalk/__init__.py), date the
#    CHANGELOG section, commit "release: prepare vX.Y.Z"
git tag -a vX.Y.Z -m "agenttalk vX.Y.Z — <summary>"
git push origin master && git push origin vX.Y.Z
# 2. publish the Release object (the easy-to-miss step):
gh release create vX.Y.Z --verify-tag --latest \
  --title "vX.Y.Z — <short description>" \
  --notes-file <CHANGELOG section for this version>
# 3. bump the README install pin (pip install ...@vX.Y.Z)
```

---

## Resuming work later (incl. on another machine)

1. Clone + editable install:
   ```bash
   git clone https://github.com/zoolok17/agenttalk.git
   cd agenttalk
   python -m pip install -e .
   agenttalk install-skills      # refresh ~/.claude/commands + ~/.codex/skills
   ```
2. Launch each agent's window **in its own directory** (see Operational
   notes), and restart the loops:
   - **Claude Code:** `/agenttalk.listen` (or `/agenttalk.sk-loop <mission>`,
     or `/agenttalk.lead` to coordinate a team)
   - **Codex:** `$agenttalk-listen` (or `$agenttalk-sk-loop`, `$agenttalk-lead`)
3. Team / fresh-review setup:
   ```bash
   agenttalk roster add claude-rev --role reviewer --group reviewers
   agenttalk roster add codex-rev  --role reviewer --group reviewers
   agenttalk broadcast --from claude-dev --to-group reviewers --kind question -m "fresh eyes on <scope>?"
   agenttalk threads --for claude-dev    # watch responded/pending
   ```

---

*Notes jointly developed by Claude Code and Codex (0.10.0–0.13.0), the
first four-agent production retro (2026-06-03), and the band's
consolidated second retro (2026-06-05: lead, codex-dev, codex-rev,
claude-rev — all four attributed).*
