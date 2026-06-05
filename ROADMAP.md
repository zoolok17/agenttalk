# agenttalk — roadmap & working notes

Carry-forward notes for the next session / next machine: what production
use validated, what hurt, and the ranked backlog to address it. Built from
the Claude Code + Codex collaboration that shipped 0.10.0–0.13.0, the first
four-agent production retro (2026-06-03), **and the band's consolidated
second retro (2026-06-05), which reprioritized everything below.**

> Current release: **v0.13.0** (`master`). See `CHANGELOG.md` for history
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

### Phase 2 — `0.14.0` "operator safety" (the band's #1 and #7)
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
3. **Intent-to-reply sugar** (#14) — `composing --to-request RID` +
   reply-in-flight visibility in `threads`/`sync` + stale-warning
   suppression. Ships in 0.14.0 **only if it stays observational and
   small** (the mechanics are ~80% built: scoped wait already honors
   request_id-tagged composing).

### Phase 2b — `0.14.x`/`0.15.0` "team scope"
4. **Role-scoped audiences + not-applicable replies** (#15) —
   `broadcast --to-role` with the audience **frozen into fan-out meta at
   send time** (never live-plumb roles into derivation; history must not
   drift), plus `reply --na` (structured meta on a normal message — it
   already closes the obligation today; a first-class kind only if that
   proves too weak).
5. **Broadcast preflight + manifest + partial-failure surfacing** (#16) —
   same release as role audiences (they expand fan-out usage).
6. **Invalid-message quarantine** (#17) — `prune --invalid` →
   `.agenttalk/quarantine/`; recoverable, never hard-delete; verified
   safe by construction (pure-function derivation, id-string cursors,
   per-message HMAC).
7. **operator_facing roster bit — advisory** (#18) — metadata + loud
   zero-or-multiple diagnostics in `doctor`/`sync`. The bus cannot
   enforce what a human sees; a half-enforced bit is worse than honest
   convention.

### Phase 3 — "trust": per-agent identity / authz RFC (#19)
**Design starts during the 0.14.0 cycle** (Codex drafts, Claude
critiques, fresh-eyes security reviewer on the draft); implementation
0.15.0 at the earliest. Scope now explicitly includes, beyond the
original threat model (key lifecycle, key↔roster mapping, signature
scope, replay, authorization policy, compromised-agent behavior):
- **global epochs / send-time barriers** — the deep end of supersession;
  the bus's first machine-checkable cross-message ordering rule;
- **retired identities / safe rename** — #9 folds in here (a retired
  identity must preserve historical validation; `send --to <old>`
  hard-fails with a hint; forwarding only as explicit opt-in; never
  rewrite history);
- **what operator_facing can mean** without real authz;
- **tool-visible next-action/owner on open threads** (the soft-deadlock
  follow-up) — explored without making the bus a workflow engine.

### Demoted / deferred
- **#9 safe rename** — stays open as the feature ask; design folds into
  the RFC (#19). Interim: documented drain-old → accept-as-old →
  re-announce pattern.
- **#11 reply-all** — deferred; role-scoped audiences + `reply --na` may
  dissolve most of the need. Revisit with production evidence after #15.
  Preserved design notes: participant set = opener + original recipients
  − self; complete `reply --dry-run --all` preflight; v1 restricted to
  non-question follow-ups.

---

## Where things stand

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
   restarts. → #18 (advisory + diagnostics; enforcement question → #19).
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
