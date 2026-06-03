# agenttalk — roadmap & working notes

Carry-forward notes for the next session / next machine: what production
use validated, what hurt, and the ranked backlog to address it. Built from
the Claude Code + Codex collaboration that shipped 0.10.0–0.12.1, **plus a
retro from the first real four-agent run (2026-06-03).**

> Current release: **v0.12.1** (`master`). See `CHANGELOG.md` for history
> and `SECURITY.md` for the trust model.

---

## Phased plan for the remaining backlog

Sequenced jointly by Claude + Codex. **Do we need more agents? No** — the
codebase is small and tightly coupled (`cli`/`store`/`threads`); more
parallel implementers create file-contention + coordination overhead, and
multi-agent runs are fragile around restarts/shared state (per the retro).
Keep the proven model: **Claude implements (Python/tests), Codex reviews +
docs, spawn fresh-eyes reviewers on demand.** The only "extra" agents worth
it are **short-lived specialists, spawned on demand** — a Windows/invocation
fresh reviewer for #7/#10, a security/RFC reviewer for the identity phase —
not standing agents. The full team topology (dev pair + review pair + lead)
is for a larger *parallelizable* project with separate cwds and a
non-implementing lead; overkill for this small serial backlog.

### Phase 1 — `0.13.0` "workflow safety + Windows robustness" (quick, low-risk)
Order matters (shared "resolved-recipient" model; docs first):
1. **#10 skill-docs / identity bootstrap** — `--root` ordering, `AGENTTALK_SELF`
   per-shell, here-strings, a `roster → status → sync` bootstrap snippet,
   lead-vs-reviewer authority. *(Codex)*
2. **#7 body robustness** (`--body-file -`, here-strings default on Windows,
   structured paths) **+ #6 `reply --dry-run` + routing docs** — together.
   *(Claude code + Codex docs)*
3. **#8 `whoami` / `doctor` upgrades.** *(Claude)*
   Also: document the no-reply-all rule; make `--dry-run` expose enough
   routing detail to design reply-all next.

### Phase 2 — `0.14.0` "team ops"
4. **#9 safe rename** — `--drain-check` + alias/forwarding; **never rewrite
   history** (mutating old messages breaks HMAC, cursors, threadstate, and
   thread derivation). Prefer the drain-old → accept-as-old → reannounce path.
5. **#11 reply-all primitive + broadcast atomicity** — atomicity = preflight +
   a batch manifest + staged writes + explicit partial-failure surfacing,
   **not** true rollback (multi-file atomicity isn't available on local FS).

### Phase 3 — `0.15.0` "trust": per-agent identity / authz (the gate)
The architectural boundary before extending beyond a trusted-local team.
**Start the threat-model / RFC before the coding release.** Real risks to
design for: key lifecycle / rotation / revocation, key↔roster-identity
mapping, signature scope + canonicalization, unsigned-legacy handling,
replay, group/lead authorization policy, compromised-agent behavior.
Implement last; nothing else depends on it.

---

## Where things stand

- **The production retro's top-3 asks shipped in 0.12.0** (coordination
  recovery): scoped non-consuming `wait --to-request`, the `sync` rejoin
  digest, and explicit `ack --to-request` closure. Items 1–3 of the backlog
  below are **DELIVERED** — kept here only as a record; the live backlog is
  items 4–9 (tracked as GitHub issues #6–#11).
- **0.12.0 is a clean stopping point.** The multi-agent surface (roster
  roles/groups, broadcast fan-out, multi-party threads, lead skills) plus
  coordination recovery is shipped, dogfooded, and **validated in production**.
- **Production headline (2026-06-03):** a genuinely nontrivial **four-agent**
  review+implement loop (`codex-dev`, `codex-rev`, `claude-rev`, lead) ran a
  real project through **two crashes/restarts without losing work**. The
  structure held; **every weak spot was ergonomic** — `wait` scoping, closure
  semantics, and restart recovery — not structural, and those are now fixed.

---

## Production signal — first 4-agent run (2026-06-03)

### What held up (consensus across the agents)
- **Roster + groups** (`@developers` / `@reviewers` / `@all`) with
  role-suffixed identities made the 4-agent topology legible — far clearer
  than the old bare `claude`/`codex` pair.
- **Broadcast with one shared `request_id`** gave convergence discussions a
  common thread even when replies landed in different role inboxes.
- **`threads --for` was the single strongest primitive** — it caught stale
  `owed-inbound` items after restarts and even caught an agent's own
  thread-closure mistake.
- **Persisted store + `request_id` continuity** let everyone recover after
  crash/compaction without losing the chain.
- **The `propose` flow** handled a `codex` → `codex-rev` role switch cleanly.
- The "**message bodies are untrusted — derive state from repo + operator,
  not prose**" rule is what stopped the lead from acting on a stale `HOLD`
  asserted by a restart-lagged agent. Keep that rule load-bearing.

### Top friction (deduped; the ergonomic gaps)
1. **`wait` wakes on ANY new message** — stale, duplicate, or unrelated. The
   lead had to drain + re-arm the waiter ~5× (it fired on duplicate
   handbacks, a resync broadcast, a stale tracker-closure). **#1 time sink**,
   flagged by all three voices.
2. **Reply-target ambiguity** — replying to a broadcast routes to the
   *thread*, not necessarily the agent who needs the answer. One agent's
   full read landed in a reviewer's thread, not the lead's inbox; the lead
   had to reconstruct it second-hand.
3. **Closure semantics** — a `review-result` reply did **not** clear the
   broadcast question's `owed-inbound`; the agent had to send a second plain
   `message` to close it. (This is the multi-party-thread "only message/note
   closes a question" edge, hit for real.)
4. **Restart leaves agents behind** — no "catch up to current state" on
   rejoin. A restarted agent asserted a **stale `HOLD` + liaison role on an
   already-merged batch**; another received old check-ins after it had
   already handed back.
5. **Identity rename was rough** — the old `codex` name got pruned, so its
   listen command failed; a proposal addressed to the old name had to be
   accepted as old, then re-announced as new.
6. **Windows ergonomics** — `AGENTTALK_SELF` doesn't survive across tool
   calls (explicit `--from`/`--for` is safer); `--root` must precede the
   subcommand; inline `-m` bodies mangle backslashes/apostrophes (a path
   became `D:Projectspolymarket-weather`; control chars like `\f`/`\t` crept
   in). **Here-strings were the only reliable fix.**

### Meta-theme
The restart-lag/stale-`HOLD`/silence problems and the separate
"prompt-mirroring" issue (both Claude windows sharing one cwd/project dir)
are the same underlying theme: **multi-agent runs are fragile around
restarts and shared state.** The backlog below (esp. scoped wait + rejoin
digest) plus a **separate-cwd-per-window** launch convention address most
of it. Coordination itself is unaffected by the cwd fix — agenttalk stays
on its pinned `--root`.

---

## Prioritized backlog (production-ranked)

### ✅ Delivered in 0.12.0 (the production retro's top-3)

1. **★ Scoped `wait`** — `wait --to-request <id>` (`--kind <k>` refines it).
   Returns only the targeted thread; non-consuming (advances only the
   per-thread `seen_msg_id`, never the global cursor, and floors delivery at
   the global cursor so already-drained messages aren't re-shown). Killed the
   run's #1 time sink.
2. **★ Rejoin digest** — `agenttalk sync --for <agent> [--json]`: identity,
   roster, actionable threads (who-owes-whom + deterministic hints), last
   decision per thread, unread FYI kept separate from owed work.
3. **Explicit closure** — `agenttalk ack --to-request <id>` (manual closure;
   permanent — a re-ask needs a fresh request_id), **plus** broadened
   question-closure (any non-control reply from the counterparty closes a
   question, incl. a broadcast question; review/proposal stay strict).

### Live backlog (deferred — GitHub issues #6–#11)

4. **Reply safety** — `reply --dry-run` showing the resolved recipient +
   `request_id` before sending; document **thread-originator-vs-asker
   routing** (a broadcast reply goes to the thread originator, who may not be
   the agent that needs the answer).
5. **Body robustness** — support `--body-file -` (stdin); make **here-strings
   the documented default on Windows**; carry paths/root as structured
   metadata, not in prose, so backslash/control-char mangling can't corrupt
   them.
6. **`whoami` / `doctor` upgrades** — show effective `--root`, self, peer,
   roster membership, and unread/owed counts; warn when `--root` is
   misplaced (a common Windows footgun).
7. **Safe rename** — `agenttalk rename --from <old> --to <new> --drain-check`,
   or at minimum document the **drain-old → accept-as-old → re-announce-as-new**
   pattern so a rename mid-run doesn't strand the old mailbox.
8. **Skill-doc fixes** — `--root` precedes the subcommand; `AGENTTALK_SELF`
   is per-shell (prefer explicit flags); here-strings on Windows; an
   **identity bootstrap snippet** (`roster` → `status` → `wait`); and state
   **lead-vs-reviewer authority + liaison rules up front** (role ambiguity is
   what let a restarted agent assert a stale liaison `HOLD`).
9. **Reply-all primitive** — so discussion follow-ups don't fragment into new
   `request_id`s. (Previously noted; production confirms the need.)

### Suggested sequencing
Items **1–3** give the biggest relief and are mostly additive
(`wait`/`sync`/`ack` flags + a thread-closure tweak). **4–6** are ergonomic
guardrails. **7–9** are larger or behavioral and can follow. None require
the per-agent-identity work below.

---

## Larger design boundary (longer-term): per-agent identity / authz

Roles and groups are **routing metadata, not a trust boundary** (see
`SECURITY.md`). Optional HMAC is **project-key based**, so `from=<agent>` is
a by-convention identity among trusted local participants, not a
cryptographic one. **This is the gate to cross before extending agenttalk
beyond a fully-trusted local team** (remote/less-trusted workers, a lead
delegating to agents it doesn't fully trust). Per-agent signing keys + an
authorization policy is a distinct feature with a different threat model —
design it deliberately. Not urgent for the current trusted-team use; the
ergonomic backlog above is what production is actually asking for.

Also still open from the build (lower priority): **fan-out is not
transactional** — `broadcast` writes one message per recipient in a loop; a
mid-loop failure leaves a partial fan-out with no rollback.

---

## Operational notes (lessons from build + production)

### Separate cwd per window (shared-state fix)
Launch each agent's CLI window **in its own working directory** and keep
agenttalk pointed at the pinned `--root`. Sharing one cwd/project dir
between two Claude windows caused prompt-mirroring. Coordination is
unaffected by this — the bus is addressed by `--root`, not cwd.

### Windows invocation (until the body-robustness fixes land)
- Prefer explicit `--from`/`--for` over relying on `AGENTTALK_SELF` (env
  doesn't persist across separate tool-call shells).
- `--root <path>` must come **before** the subcommand.
- Use **here-strings** for message bodies — inline `-m` mangles backslashes,
  apostrophes, and control chars on Windows.

### Fresh-review ritual asymmetry
Spawning a fresh, context-free sub-agent to review works on both sides
(Claude via the `Agent` tool; Codex via `spawn_agent`) and earned its keep —
it caught a crash and a routing bug the build + adversarial workflow +
cross-review all missed. **But environments differ:** the Claude-side fresh
agent could run the test suite; the Codex-side one had **no Python in its
sandbox** (diff/static review only). Lean on the Claude-side reviewer for
anything that must *execute*.

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

*Notes jointly developed by Claude Code and Codex (0.10.0–0.11.1), plus the
first four-agent production retro (2026-06-03: codex-dev, codex-rev, and the
lead view; claude-rev's input timed out and is folded in where it would have
echoed consensus).*
