# agenttalk — Release Assurance & Security Posture

**Purpose.** Every agenttalk release is attested here as **GOOD** (correct + tested),
**ROBUST** (fail-safe + adversarially probed), and **SECURE** (authority-bounded +
scanned), with the *evidence* that earns each label. This is a living document: the
release ritual appends a ledger entry per release (see **Standing commitment**).

Pairs with: `CHANGELOG.md` (what shipped) · `docs/DESIGN.md` (why / architecture) ·
`docs/ROADMAP.md` (where it's going) · `docs/ISSUES.md` (open items + accepted known-limitations) ·
`docs/audit-2026-06-28.md` (a full historical audit).

---

## 1. How a release earns GOOD / ROBUST / SECURE

Every release passes the same pipeline before it ships:

1. **Design-first.** Non-trivial or authority-sensitive work is designed before it is
   built. **Authority-/security-critical designs get an adversarial design gate** — a
   panel of independent agents that try to break the design and produce a
   PASS / PASS-WITH-CONDITIONS / REVISE verdict with concrete build conditions.
2. **Build in isolation.** Implemented in a dedicated git worktree off `master`, never
   directly on the mainline.
3. **Independent review on the final SHA.** At least **two independent reviewers**
   review the *exact* SHA that will ship (the "both-reviewers-on-the-final-SHA"
   discipline). Authority-sensitive changes additionally get **fresh adversarial
   passes** that attempt concrete exploits (double-answer, identity spoof, race,
   bypass). Reproduced evidence beats belief; any real blocker is folded and
   re-reviewed on the new SHA.
4. **Lead gate (hard, pre-merge).** `ruff` + `bandit` + `node --check` (frontend) +
   `git diff --check` + the **full pytest suite on Python 3.10 AND 3.14** — all green
   before fast-forward merge. The dev host lacks 3.14, so the lead runs it explicitly.
5. **CI matrix.** GitHub Actions runs the suite across **Python 3.10–3.13 on Windows,
   macOS, and Ubuntu**, a separate **security** workflow, and a **wheel-build /
   packaging** gate. A release ships only on green; a flake is re-run and confirmed
   (never merged red).

**Invariants enforced across releases**
- **Fail-closed authority.** The supervised executor (`supervise --drain-intents`) is
  the *sole* write boundary; browser-supplied identity is never trusted — the actor is
  derived server-side. Every write path re-validates at drain and denies on drift with
  zero sends.
- **Byte-identical read-only.** With `--enable-actions` off, the console and `/api/state`
  are byte-identical to the pure read-only dashboard (fixture-tested).
- **Idempotent + crash-safe writes.** Two-phase reconcile (attempt-floor + delivery
  fingerprint) so a crash-after-send never double-acts.
- **Signable bus + untrusted bodies.** Messages are HMAC-signable; message bodies are
  treated as untrusted data (escaped on render, never executed).
- **Documented threat-model non-goals** (see §2) — we state what we do *not* defend
  against, rather than implying we do.

---

## 2. Codebase security posture

**Last full scan: 2026-07-05** (free / open-source scanners; raw output archived by the
lead). Re-run on demand and refreshed as the codebase changes.

| Scanner | Scope | Result |
|---|---|---|
| **bandit** 1.9.4 | Python SAST | **0 issues** (28,553 LOC) |
| **semgrep** (317 rules: `p/python`, `p/security-audit`, `p/secrets`, `p/javascript`) | multi-language SAST | **0 findings** (79 files) |
| **detect-secrets** | committed credentials | **0 real** (2 hits, both intentional test-probe strings) |
| **pip-audit** | dependency CVEs | **no runtime dependencies** — agenttalk is stdlib-only |
| **vulture** | dead code | **0** (confidence ≥ 80) |
| **ruff** | lint / bug-prone patterns | clean on the enforced ruleset; expanded rulesets surface only style/robustness (e.g. message-in-`raise`), no security findings — the 3 partial-path-subprocess sites are already `# nosec`-reviewed |

**Structural security properties**
- **No third-party runtime dependency attack surface** — the package imports only the
  Python standard library; builds with `hatchling`; `dependencies = []`.
- **Authority model** — the supervised executor is the only write boundary; identities
  are resolved server-side; the bus is HMAC-signable; `.agenttalk/` state is gitignored
  and HMAC keys are generated at runtime (never committed).
- **Documented non-goals (accepted limitations, in `docs/ISSUES.md`):** the supervisor
  process-ownership model is a correctness boundary against *accidental* cross-project
  kills and PID reuse — **not** a security boundary against a deliberately malicious
  same-user process that forges command lines / parent links (plus a backward
  wall-clock step straddling PID reuse). These are explicit non-goals, not gaps.

> Coverage notes (honesty): scans are static (no DAST of the running server), use
> semgrep's community rulesets, and cover the working tree (not full git history — low
> risk given secrets are runtime-generated and never committed).

---

## 3. Per-release assurance ledger

All entries below shipped through the §1 pipeline. **CI (tests + security) is green for
every release listed** — with one honest exception: v0.64.0's `security` leg went red on a
gitleaks false-positive (a redaction-test's synthetic secret, not a real leak or code defect),
corrected in v0.64.1; and v0.69.1's `tests` leg went red on an environment-fragile `doctor` test
(it asserted the *global* `doctor` exit code / overall severity, which depend on which CLIs are
installed on the runner — a test-only fragility, not a code defect; the dev host has the CLIs so it
passed the lead gate but a clean CI runner did not), fixed test-only in the follow-up commit
`748ca74`. Reviewed-SHA = the exact code reviewed + lead-gated (fast-forward
merged); Tag = the release commit (adds version/CHANGELOG only).

### v0.69.5 — all-views dashboard render smoke + QA-skill tightening (2026-07-06)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `16d8ab5` · tag `v0.69.5`
- **Review:** first task for the newly-onboarded dedicated tester (`codex-test`, role `test-agent`) —
  it built the harness (its charter owns it) in an isolated worktree → **reviewer GO on the final SHA**
  (verified from the store). The reviewer **proved the guard across multiple views** (broke overview
  and agent-detail independently → each went red → restored green) and **confirmed no coverage
  regression** — the generalized test still catches the original v0.69.4 `st`→`info.key` agent-detail
  bug (revert → red), with the stuck-agent Restart-button assertion preserved.
- **Gate:** node --check + ruff/bandit/**gitleaks** (git-mode + range)/diff/compileall clean; pytest
  **2018 passed / 3 skipped on 3.10 AND 3.14** (clean venvs); node-absent leg confirms the smoke skips
  cleanly (1 skipped, exit 0).
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + security + wheel — green (watched post-push).
- **Robust/Secure:** display/test-only. Completes the assurance step opened in 0.69.4 — the dashboard
  JS is now *executed* across every view in the gate + CI, and the QA skills mandate smoking all views
  on shared-frontend changes. Owned and maintained by the dedicated tester going forward.

### v0.69.4 — dashboard agent-detail blank-page fix + render smoke test (2026-07-06)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `774bf9b` · tag `v0.69.4`
- **Review:** operator-reported blank agent-detail page → lead-diagnosed (undefined `st` in
  `renderAgentDetail` → runtime ReferenceError → blank render for every agent) → fix + committed
  Node-VM render smoke test built in an isolated worktree → **reviewer GO on the final SHA** (verdict
  verified from the store). The reviewer **proved the test is a genuine regression guard** — reverting
  the one-line fix turned the smoke test red (`ReferenceError: st is not defined`), restoring it turned
  it green — and flagged a real CI-safety blocker (the test errored instead of skipping when Node was
  absent), folded before GO.
- **Gate:** node --check + ruff/bandit/**gitleaks** (git-mode + range)/diff/compileall clean; pytest
  **2018 passed / 3 skipped on 3.10 AND 3.14** (clean venvs); plus a **node-absent leg** confirming the
  new smoke test skips cleanly (1 skipped, exit 0) with Node off PATH.
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + security + wheel — green (watched post-push).
- **Robust/Secure:** display-only, not authority-critical. Closes a real assurance gap — the dashboard
  JS was never *executed* by the gate (only `node --check`'d), which let a plain undefined-variable bug
  survive since v0.58.0. The committed render smoke test now runs in every gate + CI. (A dedicated
  tester and an all-views render harness are the tracked follow-up.)

### v0.69.3 — lead-chat message avatars (2026-07-06)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `c097f05` (= `e97846f` rebased zero-delta) · tag `v0.69.3`
- **Review:** operator-requested UI polish → build in an isolated worktree (frontend-only) → **both
  reviewers GO on the final SHA** (reviewer-1 security + render correctness; reviewer-2 quality/e2e —
  ran a Node DOM probe of the production renderer: operator-right/lead-left ordering, shaped avatars,
  and loading/empty/unavailable/no-avatar-fallback states all render clean). Verdicts verified directly
  from the store; rebased zero-delta onto the v0.69.2 master (range-diff `=`) before shipping.
- **Gate:** node --check + ruff/bandit/**gitleaks** (git-mode + range)/diff/compileall clean; pytest
  **2017 passed / 3 skipped on 3.10 AND 3.14** (clean venvs).
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + security + wheel — green (watched post-push).
- **Robust/Secure:** display-only, not authority-critical. Message bodies stay `textContent` (no
  `innerHTML`); avatar `src` flows only through the allowlisted avatar helpers (never built from a bus
  string); no new network surface; CSS overrides are scoped to `.tc-lead-msg-row`, so the shared
  `.tc-msg-row` (sessions transcript) render is unchanged.

### v0.69.2 — wrapper failure taxonomy + redacted dead-letter output tail (2026-07-06)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `7d6e0c3` · tag `v0.69.2`
- **Review:** design-first (codex) → lead-gated → build in an isolated worktree → **both reviewers GO
  on the final SHA** (reviewer-1 + reviewer-2, verdicts verified directly from the store). This
  security-sensitive surface (secret redaction + byte-bounding of a persisted child-output tail) took
  **four folds**, each closing a real reviewer-reproduced bug: (1) the tail leaked `Authorization`
  bearer + quoted assignment secrets; (2) *short* `Authorization` credential values still leaked; (3) a
  non-BMP Unicode line sliced on raw UTF-8 bytes could store 4100 bytes over the 4096 cap. Each fold
  added a regression and the findings narrowed to convergence — no single-reviewer path would have
  caught all three.
- **Gate:** ruff/bandit/**gitleaks** (git-mode + range)/diff/compileall clean; pytest **2017 passed /
  3 skipped on 3.10 AND 3.14** (clean venvs).
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + security + wheel — green (watched post-push).
- **Robust/Secure:** the child-output tail is **not** classification authority; secrets are redacted
  before persistence (`Authorization` bearer + assignment-style credentials); the tail is byte-bounded
  by per-character UTF-8 cost (multi-byte-safe). Infra classification is **structured-first** — a
  global-outage label requires a structured rate-limit / API-status signal, with legacy free-text
  markers demoted to an ambiguous fallback, so a local error is no longer misread as a provider outage.
  Fixes Bug 4 + Bug 5 from the 2026-07-06 wrapped-fleet incident report. New module: `redaction.py`.

### v0.69.1 — supervisor duplicate-wrapper containment (2026-07-06)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `9456cfa` · tag `v0.69.1`
- **Review:** design-first triage (codex, verified against master) → lead-gated → build in an
  isolated worktree → **both reviewers GO on the final SHA** (reviewer-1 + reviewer-2, verdicts
  verified from the store). The review caught **two real bugs** across two folds: reviewer-2 found a
  decision-event dedup gap (a steady blocked state spammed alternating `stuck_recover` /
  `launch_barrier` events), and reviewer-1 found a P1 — a barrier-held poll wrote `next_state` before
  `continue`, so it faked a launch *and* consumed the pending manual-restart request; folded via an
  explicit `barrier_state` (auto-recovery backs off without opening launch grace; manual restart
  stays unconsumed until a spawn actually passes the barrier).
- **Gate:** ruff/bandit/**gitleaks** (git-mode + range)/diff/compileall clean; pytest **2007 passed
  / 3 skipped on 3.10 AND 3.14** (clean venvs).
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + security + wheel — green.
- **Robust/Secure:** the launch barrier is **fail-closed** — an unavailable process snapshot with a
  possibly-live prior launcher blocks the replacement rather than stacking, and the barrier never
  fakes a launch or drops a manual-restart request. The `supervisor.ps1` per-project singleton lock
  (0.69.0) is unchanged; 0.69.1 only adds an advisory `doctor` warning for stale pre-lock scripts.
  Fixes Bug 2 + the Bug 3 residual from the 2026-07-06 wrapped-fleet incident report (the report's
  Bug 1 P0 and the Bug 3 new-script lock were already fixed in 0.69.0).

### v0.69.0 — dashboard liveness render for unwrapped-active agents (2026-07-06)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `aa5c85c` · tag `v0.69.0`
- **Review:** design-first (codex) → lead-gated design → build in an isolated worktree → **both
  reviewers GO on the final SHA** (reviewer-1 APPROVE, reviewer-2 GO — verdicts verified directly
  from the store, not a relayed summary) + my independent review clean. Display-only, not
  authority-critical. The review crux was **call-site classification** — verified complete: every
  agent-scoped render site (roster/counts/filter/card/detail/supervisor/avatar) uses the new
  `agentStateInfo`, while raw-state sites (timeline segments + legend) correctly retain the raw
  `stateInfo` vocabulary via a polymorphic adapter.
- **Gate:** ruff/bandit/**gitleaks** (git-mode + range)/node --check/diff/compileall clean; pytest
  **1994 passed / 3 skipped on 3.10 AND 3.14** (clean venvs).
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + security + wheel — green.
- **Robust/Secure:** a pure **client-side** presentation correction — `/api/state` is byte-identical
  (no new server field; locked by additive-boundary fixtures) and the read-only dashboard remains
  byte-identical with actions off. **Fail-closed:** an unwrapped agent shows *Active* only with a
  *fresh* heartbeat (≤120s, the shared `ACTIVE_WITHIN_SECONDS`); missing/stale/negative/invalid
  heartbeat stays *Unknown*; a known-wrapped agent (`wrapped===true`) always uses its raw health
  rendering and is never relabeled. The `agentStateInfo` matrix (fresh/stale/missing/wrapped/negative)
  is covered by an executed node-vm JS test.

### v0.68.1 — lead-chat usable with an interactive lead (hotfix) (2026-07-06)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `aad1105` · tag `v0.68.1`
- **Review:** v0.68.0 passed authority review + adversarial but shipped **unusable end-to-end** for
  the common unwrapped/interactive single-operator lead (wrapped-only liveness gate + no
  `operator_identity` on pre-0.68.0 stores). The lesson is now encoded as a **universal
  end-to-end-user-path check** in the `qa-strategy` + `tester-qa` devkit skills (for an
  operator/end-user-facing feature, run the real user journey against a realistic setup — unit /
  contract / security coverage does not substitute). The fix was reviewed by reviewer-1 (executed
  repros) + a **3-agent adversarial pass** (queue-into-void / spoofable-backfill /
  regressions+universality, all **reproduced_any = false**); reviewer-1 caught + reproduced one P2
  (the first backfill covered only the operator-facing shape, not the supported sole-lead shape) →
  folded (mirror `lead_chat_lead` inline, no `load_config` recursion) → **reviewer-1 APPROVE on the
  final SHA**.
- **Gate:** ruff/bandit/**gitleaks** (git-mode + range)/node --check/diff/compileall clean; pytest
  **1991 passed / 3 skipped on 3.10 AND 3.14** (clean venvs).
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + security + wheel — green.
- **Robust/Secure:** the liveness loosening reports available **only** for the resolved
  operator-facing/sole lead with a *fresh* heartbeat (missing/stale → unavailable), and messages
  queue durably — no queue-into-the-void. The `operator_identity` load-time inference resolves only
  to the reserved `operator` principal, only when a lead resolves and `operator` is not a roster
  agent, never overwrites an explicit value, and is in-memory only — the authenticated send path +
  the zero-fallback resolver are unchanged. **Banked** (pre-existing to the heartbeat model,
  unreachable from the real send path): future-dated heartbeat clamps to fresh; caller-overridable
  `heartbeat_stale_after`.

### v0.68.0 — lead-chat (dashboard operator↔lead direct chat) (2026-07-06)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `95875cb` (rebased zero-delta → `c34019a`) · tag `v0.68.0`
- **Review:** design-gated (PASS_WITH_CONDITIONS; 6 P1 identity conditions) → build → **three
  authority folds driven by reviewer-1's executed repros**: reviewer-1 reproduced a real
  operator-impersonation vector on the first two SHAs (queued `lead_chat_send`, then queued
  operator-*answer*, both minting an operator-sent message from the agent-writable queue) and my
  adversarial pass false-passed the first one — **the executed reviewer repro is what caught it**;
  fold #3 closed reviewer-2's P3 (malformed-input traceback). **Both reviewers GO on the final SHA**
  (reviewer-1 re-ran both repros → zero operator sends; reviewer-2 confirmed the direct-send path +
  the P3 fix). Authority-adjacent (operator as bus sender) — gated with both-reviewers-on-final-SHA.
- **Gate:** ruff/bandit/**gitleaks** (git-mode + range)/node --check/diff/compileall clean; pytest
  **1987 passed / 3 skipped on 3.10 AND 3.14** (clean venvs); clean rebase onto master `63ccf3f`
  (range-diff `=` on all 4 commits = zero logic delta).
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + security + wheel — green.
- **Robust/Secure:** operator-send authority lives ONLY in the authenticated `/api/lead-chat` request
  (loopback + CSRF + session); it is **refused from the agent-writable intent queue** (both
  `lead_chat_send` and the operator-answer path fail closed in the drain), `operator_identity()` has
  zero fallback (no self-send), the operator principal is excluded from all agents-only walks, and a
  malformed decision-answer returns a bounded 400. **Honest ceiling:** authenticated-request boundary,
  NOT a cryptographic boundary vs a fully-privileged local process — identity is an auditable
  same-machine assertion.

### v0.67.0 — mechanically-guaranteed isolated worktree per lane (2026-07-05)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `ab0cb5d` (rebased zero-delta → `27350c9`) · tag `v0.67.0`
- **Review:** design-first → **adversarial design-gate** (PASS_WITH_CONDITIONS; 12 must-fix folded)
  → build → **six review rounds** on the destructive-cleanup surface: reviewer-1 reproduced a real
  data-loss/authority vector on *every* prior SHA (waived-artifact head-mismatch → abandon live-
  worktree deletion → abandon branch-strand) and my executed-attack adversarial pass (PASS, core
  guarantees held; contributed the S-P2 primary-checkout hardening) → **both reviewers GO on the
  final SHA**. Authority-critical (first mutating git call + a new release-gate HOLD) — gated hardest
  of the run.
- **Gate:** ruff/bandit(`_git_write` `# nosec`-reviewed)/**gitleaks** (git-mode + range)/diff/
  compileall clean; pytest **1971 passed / 3 skipped on 3.10 AND 3.14** (clean venvs); clean rebase
  onto master `059a45e` (range-diff `=` on all 6 commits = zero logic delta).
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + security + wheel — green.
- **Robust/Secure:** the guarantee is **fail-closed** — provisioning is atomic under `_config_lock`
  (git error → no lane), `deliver`/`close` HOLD on any worktree-provenance mismatch, gc/abandon
  never delete an unmerged/only-ref branch (ancestor-gated) and never remove an in-use worktree.
  `_git_write` is hardened (allowlist, no shell, `--` separator, full-SHA base, prompt-disabled env,
  bounded timeout, loud fail-closed). **Honest ceiling (recorded, not over-claimed):** the delivery
  integrity token is a *store-local* HMAC — it defeats a hand-dropped artifact but is **not** a
  cryptographic authority boundary against a fully-privileged local process; on a same-machine
  cooperative bus, identity stays an auditable assertion and Git/OS is the real boundary.

### v0.66.0 — 60 shaped avatars + shape-preserving render (2026-07-05)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `3e83048` · tag `v0.66.0`
- **Review:** grounded design (a lead workflow mapped the render + allowlist + the scope decision) →
  build → **codex reviewer-1 GO** (focused: no-traversal preserved for the 60 flat names, originals/
  operator not regressed, fail-safe degrade; exact-diff review + 22 targeted tests). Display/
  preference increment — not authority-critical.
- **Gate:** ruff/bandit/**gitleaks** (git-mode + range)/node --check/diff/compileall clean; pytest
  **1949 passed / 3 skipped on 3.10 AND 3.14** (clean venvs).
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + security + wheel — green.
- **Robust/Secure:** the 60 assets are **flat `<shape>-<name>.png` exact-key allowlist entries** —
  no subdirectories, no config value joined into a served path, so the v0.65.0 no-traversal
  guarantee holds (nested/traversal probes 404). The render change is **data-driven + scoped** (a
  validated `shape` flag): originals + the operator badge keep circular rendering, so no regression.
  A bad/removed shaped id degrades to the status dot; the map is fail-soft on read.

### v0.65.1 — dashboard client-disconnect noise fix (2026-07-05)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `ff1b06e` · tag `v0.65.1`
- **Review:** grounded diagnosis (a lead 4-agent workflow mapped every `web.py` write/error path) →
  build → **codex reviewer-1 GO** (focused on the anti-masking guarantee + disconnect-classification
  completeness, no findings). Small, non-authority dashboard-robustness patch.
- **Gate:** ruff/bandit/**gitleaks** (no-git + range)/diff/compileall clean; pytest **1944 passed /
  3 skipped on 3.10 AND 3.14** (clean venvs — which also confirmed 2 supervisor-test failures in the
  builder's environment were the known stale-PATH console-script artifact, not a regression).
- **Robust/Secure:** the fix is **fail-safe without masking** — a strictly type/errno-scoped
  `_is_client_disconnect` classifier (never matches a generic `Exception`/`RuntimeError`/`ValueError`/
  `OSError(ENOENT)`) so a benign peer abort is abandoned quietly while a **real** `500` on a live
  socket still logs + returns `500` (dedicated anti-masking regression test). `handle_one_request`
  is the lifecycle chokepoint covering GET/HEAD/POST/body-read.

### v0.65.0 — agent self-selected avatars + operator avatar (2026-07-05)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `613bf1f` · tag `v0.65.0`
- **Review:** design-first (lead-gated, display/preference — not authority-critical) → build → **both
  reviewers GO** (reviewer-1 security/fail-safe + reviewer-2 correctness/contract, no findings) + a
  fresh **4-lens adversarial pass** (no-traversal / fail-safe / impersonation / additive-parity)
  that caught one real **P2 neither reviewer did** — the reserved `operator` principal key could
  collide with a roster agent named `operator` — folded (reserve the name in `validate_agent_name`)
  and re-checked GO by reviewer-2. Three P3 nits accepted/banked with rationale.
- **Gate:** ruff/bandit/**gitleaks** (no-git + range)/node --check/diff/compileall clean; pytest
  **1939 passed / 3 skipped on 3.10 AND 3.14** (clean venvs).
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + security + wheel — green.
- **Robust/Secure:** avatar serving stays **allowlist-only** — a chosen id resolves ONLY to a
  code-owned `AVATAR_ASSETS` file; no config value is ever joined into a served path (traversal,
  encoded, absolute/UNC, embedded-slash, unicode inputs all normalize to *no chosen avatar*,
  verified adversarially). A malformed/hostile `avatars` map is fail-soft (dropped with a warning,
  never bricks `load_config`/`/api/state`, never a broken image). `avatar set --from` is self-only
  (cannot relabel another principal); the avatar is display-only and never an identity/routing key.

### v0.64.1 — gitleaks security-CI false-positive fix (2026-07-05)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `b086a12` · tag `v0.64.1`
- **Review:** contained scanner-config + test-fixture hotfix (right-sized) — **codex reviewer-1 GO**
  on the final SHA (verified `[extend] useDefault = true` keeps all default rules, exactly one
  marker-only allowlist regex `sk-FAKE-AGENTTALK-…`, no `tests/` path carveout, `.gitleaksignore`
  = 3 exact `commit:path:rule:line` fingerprints only, and redaction assertions preserved).
- **Gate:** gitleaks clean in **every mode** — working-tree (`--no-git`), full 598-commit history,
  and pushed range — plus a **tightness probe** (a non-marker high-entropy `sk-` still trips; the
  marker form is allowlisted), so detection is **not** weakened. ruff/bandit/diff-check/compileall
  clean; pytest **1919 passed / 3 skipped on 3.10 AND 3.14** (clean venvs).
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + **security** + wheel — green (restores the
  v0.64.0 red `security` leg to green).
- **Process fix:** exposed a lead-gate gap — the gate had run ruff+bandit+pytest+diff but **not**
  the full CI `security` suite. The lead gate now runs **gitleaks** (and pip-audit when deps
  change) so this class is caught locally, never again only in CI.

### v0.64.0 — supervisor observability (Slice 2) (2026-07-05)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `8d0dbb7` · tag `v0.64.0`
- **Review:** gate-approved design (Track A) → build → **three untrusted-read hardening rounds**
  (secret-text leak → non-finite epochs → embedded-report leak + unguarded record — the last two
  caught by a fresh 4-lens adversarial pass that the reviewers' ring-focus missed) → **both
  reviewers GO on the final folded SHA** (each verified the redaction projections + lead-liveness
  scope). Read-only + advisory + fail-safe (not authority-critical).
- **Gate:** ruff/bandit/diff clean; pytest **1919 passed / 3 skipped on 3.10 AND 3.14** (clean venvs).
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + wheel — green. **The `security` leg went RED**
  on a gitleaks false-positive — a redaction-test's synthetic `sk-` fixture, **not** a real secret
  or code defect (the redaction behavior it tests is correct). Corrected in **v0.64.1** (above).
- **Robust/Secure:** the read/ring surfaces treat persisted + child-authored data as UNTRUSTED —
  sanitized to a token-only schema on read (non-finite rejected, secret-like free text never
  echoed, unknown keys dropped), the ring is bounded (512 / 256KB — never the accumulation class),
  and the whole surface is out-of-band (never blocks the supervisor loop or `/api/state`). Lead
  liveness is display-only with no authority reach.

### v0.63.0 — supervisor & wrapper reliability hotfix (Slice 1) (2026-07-05)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `fa9f92f` (rebased clean → `9a14d1a`) · tag `v0.63.0`
- **Review:** design-first → adversarial design-gate → build → **2 independent reviewers + a 5-lens
  adversarial reproduction pass** on the final SHA (B3 false-kill + B2 message-loss lenses confirmed
  clean; the pass + reviewer-1 found the B4 cadence bug + 2 P2s) → one fold → **both reviewers GO**,
  each confirming no over-correction (a legit current-liaison restart still relaunches). Authority-
  critical surface (restart authority + session give-up) — gated accordingly.
- **Gate:** ruff/bandit/diff clean; pytest **1902 passed / 3 skipped on 3.10 AND 3.14** (clean venvs).
  Clean rebase onto v0.62.1 — range-diff = zero Slice-1 logic delta, only the hotfix integration.
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + security + wheel — green.
- **Robust:** every failure class now has a finite, operator-visible terminal path — no silent
  infinite loop, no escalation flood; heartbeat is the liveness authority; restart authority is
  re-validated at plan time; a broken session self-heals to a fresh one. Root-cause fix for the
  spawn-loop/flood incident that struck three Claude agents this session.

### v0.62.1 — wrapper: stop the Windows pipe-teardown finalizer spam (2026-07-05)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `b1efdf2` · tag `v0.62.1`
- **Review:** contained wrapper cleanup (right-sized) — reviewer-2 GO, verified the bounded
  cleanup path (worker-stop+join, benign-teardown-suppressed close, terminate+wait, kill+5s on
  timeout — no hang) and that `make_drive` still classifies the constructor EINVAL as
  infra/retryable (not poison).
- **Gate:** ruff/bandit/diff clean; pytest **1875 passed / 3 skipped on 3.10 AND 3.14** (clean venvs).
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + security + wheel — green.
- **Robust:** `_ProcStream.__init__` is exception-safe after `Popen` — no child stdout/stdin
  pipe is left to GC finalization on any post-spawn failure, so a failing/mis-launched turn
  degrades quietly instead of spamming `EINVAL`. Silences the *symptom*; the root-cause
  broken-session spiral is fixed by the supervisor reliability work (in review).

### v0.62.0 — assurance-scan: codebase-adaptive evidence producer (Slice A) (2026-07-05)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `73c4a85` · tag `v0.62.0`
- **Review:** design-first → two adversarial design-gates → build → **three independent
  tracks on the final SHA** — reviewer-1 GO (zero findings) + fable GO + a fresh 6-lens
  adversarial reproduction pass (GO_WITH_NITS; all 8 original fail-opens confirmed closed,
  no over-correction, no regression). One consolidated fold + a micro-fold closed every
  finding; both design-gates each caught an incident-validated bug before ship.
- **Gate:** ruff/bandit/diff/compileall clean; pytest **1872 passed / 3 skipped on 3.10 AND
  3.14**, run in clean isolated venvs (the gate host had a stale editable shadow; a clean
  env was stood up so the gate ran against the reviewed code, not a shadow).
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + security + wheel — green.
- **Robust/Secure:** pure evidence producer (never mutates the repo, never decides GO/HOLD);
  fail-safe gate + fail-honest per-dimension attestation (a skipped required
  security/deps/secrets scan → SECURE=UNKNOWN, never silently good); fail-closed manifest;
  self-waiver guard; provenance guards against green-about-wrong-code. NOTE: once this is
  wired into the gate/close (Slice B), SECURE for *this* repo will honestly read UNKNOWN
  until gitleaks + a dependency scanner are configured in the release profile.

### v0.61.0 — Team Console: colored flow + full history + archived + avatars (2026-07-05)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `9dd0342` · tag `v0.61.0` (`60f808b`)
- **Review:** reviewer-1 + reviewer-2 GO on the final SHA (focus: `/api/state` derivation
  parity + the new read-only `/api/threads` endpoint fail-safe + frontend contract).
- **Gate:** ruff/bandit/node/diff clean; pytest **1835 passed / 3 skipped on 3.10 AND 3.14**.
- **CI:** tests matrix (3.10–3.13 × win/mac/ubuntu) + security + wheel — green. One
  3.10-Windows timing flake in an *unrelated* work-heartbeat test; re-run green (hardening tracked).
- **Robust/Secure:** read-only, additive; one shared thread classifier keeps `/api/state`
  byte-identical (parity-tested); the endpoint is fail-safe (an error can never affect
  `/api/state`); avatars served from a fixed allowlist (no path traversal).

### v0.60.2 — console UX fixes: composer poll-clobber + flow-line thickness (2026-07-04)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `effbc58` · tag `v0.60.2` (`045a114`)
- **Review:** reviewer-1 GO (frontend-only, right-sized).
- **Gate:** ruff/bandit/node/diff clean; pytest **1832 / 3 skipped on 3.10 AND 3.14**. CI green.
- **Robust/Secure:** frontend-only (`console.js`); actions-off behavior unchanged.

### v0.60.1 — cross-path operator-answer dedup (2026-07-04)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `d6bab84` · tag `v0.60.1` (`62f1c4c`)
- **Review:** reviewer-1 + reviewer-2 GO **+ a fresh adversarial concurrency pass that
  could-not-break** (8-way race + mixed drain/relay race + 30 stress iterations, all single-send).
- **Gate:** clean; pytest **1832 / 3 skipped on 3.10 AND 3.14**. CI green.
- **Robust/Secure:** both operator-answer paths serialize on one atomic check-and-send
  under the non-reentrant config lock (verified deadlock-safe); fail-closed on
  lock/read/mismatch/reject.

### v0.60.0 — operator inbox: answer escalations from the browser (2026-07-04)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `6827801` · tag `v0.60.0` (`f65c2fe`)
- **Review:** **adversarial design gate = PASS-WITH-CONDITIONS (8 conditions folded)**;
  then a **4-way review on the final SHA** — 2 reviewers GO + 2 fresh adversarial passes
  (authority and idempotency/byte-identity) **could-not-break** — plus a re-review of the
  coalescing fold (both GO).
- **Gate:** ruff/bandit/node/diff clean; full suite green on 3.10 AND 3.14. CI green.
- **Robust/Secure:** browser→lead-answer is authority-sensitive; the executor re-verifies
  the escalation is pending + owed to the resolved actor, never self-answerable, never a
  coalesced duplicate; server-injected operator meta; two-phase reconcile prevents
  double-answer; actions-off byte-identical.

### v0.59.3 — Team Console per-provider capacity + compact density (2026-07-04)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `2695266` · tag `v0.59.3` (`75c818f`)
- **Review:** both reviewers GO on the rollout-fix fold (one reproduced-P1 held and folded
  first — reproduced-evidence-trumps-belief).
- **Gate:** clean; pytest **1813 / 3 skipped on 3.10 AND 3.14**. CI green.
- **Robust/Secure:** capacity read is bounded + fail-closed (degrades to *unknown* rather
  than reporting a stale value as observed); supervised Codex reads only its isolated home.

### v0.59.2 — supervisor process-ownership attribution (cross-project-kill P0) (2026-07-04)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `5add1d0`/`1dd10ba` · tag `v0.59.2`
- **Review:** **end-the-class design via an adversarial 4-lens design gate
  (PASS-WITH-CONDITIONS)** after 3 patch rounds each caught a deeper residual; then
  reviewer-2 GO + multiple fresh adversarial passes could-not-break.
- **Gate:** ruff/bandit clean; **1790 passed on 3.10 AND 3.14**. CI green.
- **Robust/Secure:** typed process-ownership + strict-live-chain + launch-nonce
  confirmation; fail-safe (never cross-kills); monotonic-clock / cmdline-spoof documented
  as non-goals.

### v0.59.1 — Team Console write-spine hardening (2026-07-04)
**GOOD ✓ ROBUST ✓ SECURE ✓** · reviewed-SHA `9d88dc1` · tag `v0.59.1`
- **Review:** claude-reviewer-3 GO (per-test pass-for-right-reason analysis) + codex
  security review; a reproduced release-blocker P1 was folded before ship.
- **Gate:** full suite green on 3.10 AND 3.14. CI green.
- **Robust/Secure:** drain-time frozen-plan revalidation (deny-on-drift, zero sends),
  pid-start-aware anti-reuse reclaim, torn-intent quarantine, negative/e2e regression suite.

### Releases ≤ v0.59.0
Shipped through the same review + gate + CI discipline. Their evidence lives in
`CHANGELOG.md`, `docs/ISSUES.md` (audit-findings disposition), `docs/audit-2026-06-28.md`
(a full independent audit), and the git tag/CI history. Not re-attested per-release here.

---

## 4. Standing commitment

**Every release appends its own ledger entry to §3 as part of the release ritual**, and
refreshes §2 if the scan surface changed. Ship sequence:

> bump `__init__`+`pyproject` → `CHANGELOG.md` → **`docs/ASSURANCE.md` ledger entry** →
> `README` pins → tag → push → **CI green** → GitHub release.

An entry is only valid if it records: the reviewer verdicts (on the final SHA), the
lead-gate result (pytest on 3.10 **and** 3.14, ruff/bandit/node/diff), the CI matrix
status, any adversarial-pass outcome, and any new known-limitation. If a release cannot
truthfully claim GOOD/ROBUST/SECURE with that evidence, it does not ship.
