# Changing the gateway's spending limits without a new ledger

Status: proposed (issue #396). Nothing in this document is built yet. Every command, field and table described below is a proposal.

**In plain words:** the paid gateway's three spending limits are fixed when its ledger is made, so today every change means a 15-20 minute supervised window that sets the ledger up again. This design proposes three small steps instead.
1. Lowering a limit becomes one instant command that needs no special proof, because it can only make spending stricter.
2. The operator gets a secret of their own, a passphrase that no paid seat holds. Raising a limit back up, within what the operator authorised when the ledger was made, then needs only that passphrase.
3. Changing what was authorised, for example raising the ceiling beyond it, becomes one guided command that pauses new spending, keeps the ledger and its history, and can always be finished or undone after an interruption.

The design decides who may raise a limit, which changes come first, and what each change does if it is cut off half way. It does not promise that a provider can never charge more than expected, and it does not enforce one budget across two machines; it says exactly what it does promise instead. It leaves open a few choices for the lead and the operator, listed at the end.

Audience: the operator and the lead, who decide; the builder and the reviewers, who need the contracts and the recovery tables. Prepared on 2026-10-07 (work order tk-5b5310592377), following two challenge verdicts (ch-limits-a, ch-limits-b), both "reshape", both accepted by the lead. Code read at master 33b5f9aba4dfd7df4275e3838b1f39254c1ad4e5.

## Contents

1. [The problem today](#the-problem-today)
2. [What this design promises, and what it does not](#what-this-design-promises-and-what-it-does-not)
3. [The plan at a glance](#the-plan-at-a-glance)
4. [Step 1: lowering a limit (ships first)](#step-1-lowering-a-limit-ships-first)
5. [Step 2: the operator's own proof, and raising back up](#step-2-the-operators-own-proof-and-raising-back-up)
6. [Step 3: changing what was authorised](#step-3-changing-what-was-authorised)
7. [The ceiling is special](#the-ceiling-is-special)
8. [Two machines](#two-machines)
9. [State-transition and recovery tables](#state-transition-and-recovery-tables)
10. [Contracts](#contracts)
11. [Proof plan](#proof-plan)
12. [Sizes](#sizes)
13. [Until this ships: the re-init scripts as the interim tool](#until-this-ships-the-re-init-scripts-as-the-interim-tool)
14. [Open questions](#open-questions)
15. [Technical notes](#technical-notes)

## The problem today

The gateway keeps one ledger per machine, holding three limits:

- **The ceiling:** the most this machine may spend in total, counted over all months together. It describes this machine's share of the provider account.
- **The cutoff:** the most it may spend in one month (in the first month, on top of the opening balance).
- **The soft stop:** a warning figure below the cutoff. It is shown, but nothing enforces it.

All three are part of the ledger's **price policy**. That is one record of the prices, the reservation rule and the three limits, and its fingerprint (a SHA-256 hash) is stored in five places that must agree:
- the ledger's own metadata;
- the ledger's install marker;
- the gateway's install manifest;
- the scheduled task's saved identity;
- the running gateway's runtime marker.

The last accepted check call (the "dashboard canary") is also tied to that fingerprint. If any of these disagree, the gateway refuses to run. That refusal is deliberate: it is how the gateway notices tampering or a half-finished change.

So today a new limit means a new ledger: stop, back up, move the old ledger aside, set up again, reinstall the task, start, make a new canary call, and restart the paid seats (`docs/STEP-ENVELOPE-SERVICE-READERS.md`, "Upgrade path"). It works and it is safe, but it is a supervised window of 8-9 steps, about 15-20 minutes of the operator's time each time. It also starts a new ledger, so the spending history stays behind in a backup. The budget has changed three times in about two weeks.

The challenges found three things a simpler change must not get wrong:
- **Who may raise a limit.** The only proof the gateway checks today is its front token, and every paid seat is given that same token as its login key. So anything that accepts the front token as authority would let a paid seat raise its own budget.
- **What a half-finished change leaves behind.** A change touches several places that must agree.
- **What can honestly be promised about overcharges and about two machines.**

## What this design promises, and what it does not

**It promises:**
- **New spending respects the authorised local limits.** Every new reservation checks the limits in force at that moment, as today.
- **Nothing raises a limit without the operator's own proof.** Nothing raises one automatically either.
- **Lowering a limit never strands a turn in progress.** Calls already admitted settle normally.
- **No change clears a hold.** A hold placed for any reason stays until it is cleared on its own terms.
- **The ledger and its history are kept.** Every change is recorded: when, what kind, the old and new values, whether the operator's proof was given, and the reason.
- **A change cut off at any point leaves a state that the status names,** and one command finishes it or undoes it.

**It does not promise:**
- **That the provider can never charge more than expected.** If the provider reports more use than a call reserved, the full charge is recorded, the gateway puts itself on hold, and no further call is admitted until the operator looks. That is today's behaviour, and it stays. No limit can undo a charge that already happened.
- **That two machines together stay within one budget.** Each machine's ledger knows only itself. Dividing the budget between them is the operator's decision, recorded on each machine; nothing checks the sum across machines (see "Two machines").
- **Protection against a program that edits the ledger file directly.** All seats run as the same user as the operator, so such a program could rewrite any file agenttalk keeps, today and after this change. The proof protects the commands; it is not a lock on the files.

This narrows one sentence in issue #396: "spending can never go above what the operator authorised, including when two machines each run their own gateway". That promise is replaced by the first list above, for the reasons in the second.

## The plan at a glance

| Step | What it adds | Needs the operator's proof? | Needs a gateway window? | Size |
| --- | --- | --- | --- | --- |
| 1 | lower the cutoff or the soft stop, instantly, while the gateway runs | no: it only makes spending stricter | once, to install the feature | M |
| 2 | the operator's passphrase; raise a lowered limit back up, up to what was authorised | yes, for every raise | no | M |
| 3 | change what was authorised: raise beyond it, or change the ceiling | yes, for any raise | no: the command pauses, stops and restarts the gateway itself | L |

The key idea is to separate two figures for each limit:
- **The authorised limit:** what the operator authorised when the ledger was made, or in a later step-3 change. It is part of the price policy and its fingerprint, as today.
- **The working limit:** what the gateway enforces now. It is never higher than the authorised limit, and it is not part of the fingerprint.

Steps 1 and 2 move only the working limits, so they touch none of the five fingerprint places and need no canary. Step 3 moves the authorised limits, and is the only step that rewrites the fingerprint.

In practice, the operator can authorise generously once (step 3, or at a re-init) and then steer the working limits up and down with steps 1 and 2, with no window at all. Tomorrow's re-init already sets the desktop's authorised ceiling to 212 EUR. Once steps 1 and 2 are built, every later change within that amount needs no window.

## Step 1: lowering a limit (ships first)

**In plain words:** the operator, or the lead, can lower the cutoff or the soft stop at any time, while the gateway runs, with one command. The change takes effect from the next call. Calls already admitted finish normally, and nothing is undone. Lowering needs no special proof, because it can only make spending stricter, but it is always recorded.

**What the operator sees:**

```text
agenttalk gateway limits
  authorised: ceiling 212.00, cutoff 203.00, soft stop 193.00 EUR (policy <fingerprint>)
  working:    ceiling 212.00, cutoff 203.00, soft stop 193.00 EUR
  this month: spent 31.20, unresolved 0.23; room under the working cutoff 171.57
  all months: spent 72.40 incl. opening 7.60; room under the ceiling 139.37
  version:    lv0   (no change since the ledger was made)
agenttalk gateway limits lower --cutoff-eur 120 --soft-stop-eur 100 --expect lv0 --reason "slow down until Friday"
  lowered: cutoff 203.00 -> 120.00, soft stop 193.00 -> 100.00 EUR; version lv1; recorded as change 1
```

(Proposed output, not runnable yet; the amounts are an example.)

**The rules,** checked inside one ledger transaction, under the same write lock every reservation takes:
1. **`--expect` must name the current version.** A stale or repeated command finds a newer version and is refused, with nothing changed. The refusal shows the change that happened since.
2. **The new working values may not go up,** and must satisfy soft stop < cutoff <= ceiling.
3. **The new cutoff may not be below what this month is already committed to:** this month's spend, plus every unresolved reservation, minus the opening allowance in the first month. Such a request is refused and the numbers are shown. To stop all spending at once, the operator uses a hold, which exists today.
4. **Holds are left exactly as they are.** A ledger on hold can still be lowered.
5. **The usual clock checks apply.** A clock that went backwards refuses the change.

**What it changes:** the working values, the version, and one new row in the change history. That is a single SQLite transaction: an interruption leaves either the old state or the new one, never a mix. It changes no fingerprint, so the running gateway, the manifest, the task, the runtime marker and the canary are untouched.

**The one-time install, and why.** agenttalk 0.97.0 does not know about working limits. If an old version opened a ledger with a lowered working cutoff, it would silently enforce the higher authorised one. So step 1 ships with a one-time migration, `gateway limits-install`, run once in a gateway window. It moves the ledger to the next schema version, which every older version refuses, the same fence `binding-install` used. It starts the working values equal to the authorised ones, at version lv0. This is the "one more gateway window, once" that issue #396 already expects.

## Step 2: the operator's own proof, and raising back up

**In plain words:** the operator chooses a passphrase that no seat is ever given. agenttalk keeps only a slow-to-guess fingerprint of it in the ledger. Every raise asks for it at the keyboard. With it, the operator can raise a lowered working limit back up, as far as the authorised limit, at once and without a window.

**Why not the front token.** The front token is the only secret the gateway checks today (`_operator_credential` reads it from its file). It is also handed to every paid seat as its login key, so a seat holds it. A proof for raises must be something a paid seat does not hold.

**The passphrase:**
- **Set once, at the keyboard:** `agenttalk gateway operator-proof set`. It asks for the passphrase twice, through the console's hidden-input prompt. It refuses to run when input does not come from an interactive console, and it never takes the passphrase as an argument or from an environment variable. That is what keeps it off command lines and out of process listings.
- **Stored as a fingerprint only:** a scrypt digest (Python's standard library) with its own random salt and cost settings, in the ledger's metadata. It is covered by the ledger's integrity check, like the limits. The passphrase itself is never written, logged or echoed.
- **Checked on every raise:** the command asks for it at the prompt and compares the fingerprint in constant time. A wrong passphrase is refused, with nothing changed, and the failed attempt is recorded. After five failed attempts in one hour, raises are refused for an hour.
- **Changed:** `gateway operator-proof change` asks for the current passphrase first.
- **Forgotten:** there is no recovery command. The operator falls back to a re-init window, which makes a new ledger without a passphrase, and sets a new one. This is on purpose: a recovery command would be a second way in.

**Setting it up on an existing ledger, without a re-init.** The passphrase lives in ledger metadata, so setting it needs no new ledger. The first `set` is allowed only while no passphrase exists. It is recorded in the change history with its time, and `gateway status` shows "operator proof: set on <date>" or "operator proof: not set". The plan is to set it in the same window that runs `limits-install`, so a seat never gets the chance to set one first.

Seats run as the same user as the operator, so a seat could also run the first `set`. It would have to type at an interactive console, which a seat does not have. Even so, the only lasting protection is that the operator sets the passphrase in the install window and checks the status line afterwards. A proof the operator did not set means: stop and re-init.

**What it protects, and what it does not:**
- **It protects against a raise through agenttalk's commands by anything that does not know the passphrase.** That includes a paid seat that decides on its own to give itself more budget.
- **It does not protect against a program that reads the operator's keystrokes, or that edits the ledger file directly.** The ledger file also holds the fingerprint, so a program that can read the file could try to guess the passphrase offline. The scrypt cost slows guessing; a passphrase of at least four random words keeps it out of reach.

**Raising a working limit back up:**

```text
agenttalk gateway limits raise --cutoff-eur 203 --expect lv1 --reason "back to normal"
  operator passphrase: ********
  raised: cutoff 120.00 -> 203.00 EUR (authorised 203.00); version lv2; recorded as change 2 (proof given)
```

(Proposed output, not runnable yet.)

The same rules as step 1 apply, with three differences:
- the values may go up, but never above the authorised ones;
- the passphrase is required;
- the change record says the proof was given.

Like step 1, this is one transaction, and no fingerprint changes.

**Step 2 also decides clearing a hold.** Today `gateway clear-hold` asks for no proof at all. Clearing a hold loosens spending just as a raise does. This design recommends that clearing a hold require the operator's proof once step 2 exists. That is a decision for the lead (see "Open questions"), and it can ship with step 2 or later.

## Step 3: changing what was authorised

**In plain words:** when the operator wants more than they authorised before, for example after adding money to the provider account, one command changes the authorised limits on the same ledger. It shows the old and new figures and asks for the passphrase. It pauses new spending and lets running calls finish. It then stops the gateway, updates the five places that carry the price fingerprint in a fixed order, starts the gateway again and makes one small check call. Then it releases the pause. If anything interrupts it, the gateway stays paused and says so, and one command finishes or undoes the change.

**Which changes need step 3:**
- any authorised limit raised above what was authorised before;
- any change to the authorised ceiling, up or down;
- a lowering of an authorised limit that the operator wants recorded as the new authorised value, not just as a working value. This is rare: step 1 covers ordinary lowering.

**The one operation,** with each phase recorded in the ledger as it happens:

| Phase | What happens | Durable write? |
| --- | --- | --- |
| P0 preview | prints the old and new authorised and working limits, the room left this month and in total, the declared machine allocation, whether the proof is needed and whether the change is allowed. Writes nothing. | no |
| P1 confirm | checks `--expect` against the current version; asks for the passphrase when anything goes up | no |
| P2 pause | records a pending change (its id, the expected old fingerprint, the new limits and fingerprint, the phase) and, with it, a pause that refuses new reservations. This is a separate flag: an existing hold is not touched. | yes, ledger |
| P3 drain | waits for every admitted call to settle normally, up to 10 minutes. An unresolved call that needs the operator's reconcile ends the change here, with nothing else changed. | no |
| P4 stop | stops the gateway with the existing stop command | the task state |
| P5 ledger | in one transaction: the new authorised limits and fingerprint; the working limits set to match, unless the operator gave lower ones; the current canary moved, unchanged, into a canary history row bound to the old fingerprint; one change-history row; the phase set to "ledger-done" | yes, ledger |
| P6 marker | rewrites the install marker with the new fingerprint, by atomic replace | yes, file |
| P7 manifest | rewrites the install manifest with the new fingerprint, as `reconfigure` does today | yes, file |
| P8 identity | rewrites the task's saved identity, as `task-install` does today for an unchanged task | yes, file |
| P9 start | starts the gateway, which writes its runtime marker with the new fingerprint | yes, file |
| P10 canary | one small paid check call through the gateway, accepted on the ledger's own figure (the operator's rule since 2026-09-22). The pause stays on and admits exactly this one call: the command opens a one-call slot bound to a one-time value that only it holds and sends with the call, and the slot closes after one reservation, used or not. | yes, ledger |
| P11 release | removes the pending change and the pause | yes, ledger |

**Why the canary moves and a new one is made.** The canary record says "this call, priced under this fingerprint, matched the bill". After P5 the fingerprint is new, and the old canary cannot be rewritten to claim it was taken under the new one. So it is kept, unchanged, as history, and P10 makes a fresh one. It costs about one cent and one short call.

**Why the pause must stay on until the canary is accepted.** Today a missing canary stops a seat only when it starts. The ledger itself still admits calls without one, which is how the canary call gets through. A re-init never meets this gap, because its new front token forces every seat to restart. Step 3 keeps the token, so the paid seats keep running. If the pause were lifted before P10, they could spend under a fingerprint that no canary has checked yet. Hence the one-call slot.

A later improvement could let a canary carry over when only the limits changed and the prices did not. That is optional (work order 4 under "Sizes"), and it would be a new trust rule that needs its own review.

**Who gets the change:**
- the gateway, restarted in P9;
- the paid seats: they do not need restarting, because the front token does not change;
- calls admitted before P2: they were drained in P3, and they settled under the old fingerprint, which their records keep.

**What step 3 refuses** (in P0 or P1, before any write):
- a stale `--expect`;
- a raise without the passphrase, or with a wrong one;
- a new ceiling below everything already committed plus unresolved;
- a new cutoff below this month's commitments;
- any combination that breaks the ledger's own rule, opening + cutoff + one reservation <= ceiling (checked on every ledger read today);
- a ceiling raise without the account statement (see "The ceiling is special");
- a second change while one is pending.

**What step 3 does not change:**
- the per-turn spending cap, which init set and which has its own fingerprint (see "Open questions");
- the token files;
- the config;
- the scheduled task's program;
- any hold. A hold placed before or during the change is still there at the end, and the gateway still refuses calls until it is cleared.

## The ceiling is special

The ceiling is not a speed setting. It describes this machine's share of the provider account, and the code calls it the external account ceiling. Raising it in the ledger adds no money to the account: if the account itself has no more money, a higher ceiling only means the provider refuses first instead of the gateway.

To keep that clear, the design has five parts:
- **Its own name in commands and status:** "this machine's share of the provider account (ceiling)". It is never just "limit".
- **Any ceiling raise requires `--account-budget-eur <total>`:** the operator's statement of the whole account's budget across all machines. With it comes `--account-raised` when that budget itself went up, a confirmation that the money was added at the provider first. Both are stored in the change record.
- **The preview says it in words:** "raising this number does not add money to the provider account".
- **This machine's share may not exceed the stated account budget.** The preview subtracts the other machines' declared shares (next section).
- **Lowering the ceiling needs no proof,** but it still goes through step 3 in this design, because the ceiling is part of the fingerprint. "Open questions" suggests also giving the ceiling a working value, so that lowering it becomes as instant as lowering the cutoff.

## Two machines

The desktop and the VM each run their own gateway with their own ledger. Neither can see the other's spending, and this design does not try to make them.

- **The allocation is the operator's explicit decision, recorded on each machine.** Every step-3 change, and every step-2 raise, carries `--account-budget-eur` (the whole account) and `--other-machines-eur` (the shares the operator gives the other machines). The record keeps both. The preview shows "account 300; this machine 212; others declared 40; unallocated 48".
- **Moving budget from one machine to the other is done in two steps, donor first:**
  1. On the donor machine, the operator lowers its limit; no proof is needed. The command prints a change receipt: one line with the machine's ledger generation, the change number, the old and new values and a short fingerprint.
  2. On the recipient machine, the operator raises its limit with the passphrase and passes that receipt with `--donor-receipt`. The receipt is recorded as the reason the money is free.

  The recipient cannot verify the receipt, because it cannot reach the donor's ledger. The record therefore says "declared by the operator, not verified".
- **An unreachable donor is not proof its share is free.** Without a receipt, a raise that would take this machine above its previous declared share is refused. The exception is an `--account-budget-eur` increase marked `--account-raised`: then the money is new, and no donor gives anything up.
- **No command claims to enforce a global sum.** The status always says "allocation declared by the operator" next to these figures.

## State-transition and recovery tables

These tables come before any build. Each must hold, with a test (see "Proof plan").

### A. Where the price fingerprint lives, and the order it changes in (step 3 only)

| # | Place | What it holds | Written in phase | Checked by |
| --- | --- | --- | --- | --- |
| 1 | ledger metadata | the authorised limits and the price fingerprint, recomputed and compared on every open | P5 | every ledger read (`_verify_metadata`) |
| 2 | ledger metadata, canary | the accepted canary must have been settled under the current fingerprint | P5 (moved to history), P10 (new) | every ledger read |
| 3 | install marker (`install.json`) | the price fingerprint | P6 | every ledger read: it must equal #1 |
| 4 | install manifest (`.agenttalk/gateway/install-manifest.json`) | the price fingerprint | P7 | gateway start and status |
| 5 | task identity (`.agenttalk/gateway/task-identity.json`) | the price fingerprint | P8 | gateway start and status |
| 6 | runtime marker (`.agenttalk/gateway/runtime.json`) | the price fingerprint and the per-turn cap fingerprint, written by the running gateway | P9 | gateway status |
| - | each provider call's row (`attempts.policy_hash`) | the fingerprint the call was admitted under | never rewritten | history only |

Steps 1 and 2 change none of these rows. They change only the working limits, the version and the change history, all inside the ledger.

### B. Step 3: an interruption after each durable write

"Status" is what `gateway status` and `gateway limits` would show. **Resume** means `gateway limits resume <id>`, which continues from the recorded phase. **Abort** means `gateway limits abort <id>`, which goes back to the old authorised limits.

| Interrupted after | State | Spending | Status | Resume | Abort |
| --- | --- | --- | --- | --- | --- |
| P2 pause | pending change recorded; nothing else changed | new calls refused (paused); admitted calls settle | "limit change <id> paused, not applied" | goes on with P3 | removes the pause and the pending change; old limits stay in force |
| P3 drain, timed out | as P2 | as P2 | as P2, plus "N calls still unresolved" | waits again | as P2. The command aborts by itself on its own timeout. |
| P4 stop | gateway stopped; ledger unchanged | none (stopped) | "limit change <id>: gateway stopped, ledger unchanged" | goes on with P5 | removes the pending change, starts the gateway, removes the pause |
| P5 ledger | new fingerprint in ledger metadata; marker still old | none: every reader refuses the ledger, because #1 and #3 disagree. That is today's fail-closed check, and it stays. | "limit change <id>: ledger updated, marker pending". A dedicated read accepts exactly this pair, and only while the pending change names both fingerprints. | rewrites the marker (P6), then P7 to P11 | restores the old limits and fingerprint in one transaction and moves the canary back from history (it is unchanged); then starts the gateway and removes the pause |
| P6 marker | ledger and marker new; manifest and identity old | none (stopped); the start refuses the mismatched manifest | "limit change <id>: manifest pending" | P7 to P11 | writes the marker back, then as P5 |
| P7 manifest | ledger, marker and manifest new; identity old | none (stopped) | "limit change <id>: task identity pending" | P8 to P11 | rewrites the manifest back, then as P6 |
| P8 identity | all files new; gateway stopped | none (stopped) | "limit change <id>: ready to start" | P9 to P11 | rewrites the identity back, then as P7 |
| P9 start | gateway running under the new fingerprint; no canary | new calls refused (the pause holds); a seat that tries to start refuses (canary missing) | "limit change <id>: waiting for the canary" | P10, P11 | stops, then a full abort as P8 |
| P10, slot open, before the call settles | the one-call slot is used or still open | only the command's own canary call; the slot closes after one reservation | "limit change <id>: canary call in progress" | settles the call, or reconciles it as any unresolved call; then a fresh slot, P10 again | after the canary call is resolved: stops, then a full abort as P8. The canary call's row stays in the ledger, under the new fingerprint. |
| P10 canary | new canary accepted; pause still on | still paused | "limit change <id>: applied, releasing" | P11 | not offered: the change is complete. To go back, run a new step-3 change to the old values; lowering needs no proof. |
| P11 release | done | normal, under the new limits | the new limits and the change row | - | - |

**Five rules hold in every row:**
- **No state admits a call under limits that are partly applied.** Either the pause or the stopped gateway prevents it, and from P5 to P6 the ledger itself refuses.
- **Abort never clears a hold.** It removes only its own pause.
- **Abort never rewrites history.** The change-history row of an aborted change stays, marked "aborted", and so does the canary history.
- **A second change cannot start while one is pending.**
- **Resume and abort refuse a pending change that does not match their checks:** the expected fingerprints, the change id and the ledger generation. Such a change needs the lead.

### C. Steps 1 and 2: the single-transaction changes

| Situation | Result |
| --- | --- |
| interrupted before the commit | SQLite rolls the transaction back: the old working limits, the old version, no history row |
| interrupted after the commit, before the output | the change is applied. Repeating the command finds a newer version and is refused as stale, and `gateway limits` shows the recorded change. |
| repeated command, same `--expect` | refused as stale; nothing changed |
| a request below this month's commitments (or below total commitments, for a ceiling working value) | refused, with the numbers; nothing changed; any hold stays |
| a raise without the passphrase, or with a wrong one | refused; the failed attempt is recorded; nothing else changed |
| the ledger on hold | lowering allowed, raising allowed with the proof; the hold stays in both cases |
| unresolved calls | allowed; they count in the floor and settle normally |
| a step-3 change pending | refused: one change at a time |
| an older agenttalk opens the ledger | refused by the schema fence from `limits-install`, so it can never ignore a lowered working limit |

### D. The questions the challenges asked

| Question | Answer in this design |
| --- | --- |
| Authority: who may raise? | only the holder of the operator's passphrase, typed at a console; never the front token, which every paid seat holds |
| Admitted calls | they keep the reservation they were admitted with and settle normally. In step 3 they are drained before anything changes. |
| A request lower than what is already owed | refused, with the numbers, without touching any hold. A hold is the tool for "stop now". |
| Holds | no change places or clears one. The step-3 pause is its own flag, removed only by its own release or abort. |
| Historical canary evidence | never rewritten. Steps 1 and 2 keep the same fingerprint, so the canary stays valid as it is. Step 3 moves it, unchanged, into history under its old fingerprint, and makes a new one. |
| An interruption between writes | table B for step 3; single transactions for steps 1 and 2 |
| The places the fingerprint lives | table A, with the order P5, P6, P7, P8, P9 |

## Contracts

All proposed; the names may change in review.

### Commands

| Command | Writes | Proof | Exit codes |
| --- | --- | --- | --- |
| `gateway limits [--json]` | nothing | no | 0; 2 when the ledger is unreadable |
| `gateway limits-install` | the schema fence, the working limits = authorised, lv0 (one window, once) | no; the gateway must be stopped | 0 installed or already installed; 2 refused |
| `gateway limits lower --cutoff-eur X [--soft-stop-eur Y] --expect lvN --reason TEXT` | working limits, version, history row | no | 0 lowered; 2 refused (stale, upward, below commitments, pending change) |
| `gateway operator-proof set` / `change` | the proof fingerprint, a history row | `change` needs the current one | 0; 2 refused (not a console, already set, mismatch) |
| `gateway limits raise --cutoff-eur X [--soft-stop-eur Y] --expect lvN --reason TEXT [--account-budget-eur T --other-machines-eur O]` | working limits up to the authorised ones, version, history row | yes | 0 raised; 2 refused; 4 proof refused |
| `gateway limits change --ceiling-eur Z --cutoff-eur X --soft-stop-eur Y --expect lvN --reason TEXT --account-budget-eur T --other-machines-eur O [--account-raised] [--donor-receipt R] [--yes]` | step 3, phases P2 to P11 | yes, if anything goes up | 0 applied; 2 refused before any write; 3 interrupted, the pending change is named; 4 proof refused |
| `gateway limits resume ID` / `abort ID` | continue or undo a pending step-3 change | `resume` asks for the proof again if the change raises anything | 0; 2 refused (no such change, mismatch) |

`--yes` skips the final "apply? [y/N]" question after the preview. The passphrase prompt is never skipped.

### What the ledger keeps (new, all fenced by `limits-install`)

- **Metadata:**
  - `working_trial_cutoff_micro_eur`, `working_soft_stop_micro_eur`; optionally `working_external_ceiling_micro_eur` (see "Open questions");
  - `limits_version`;
  - `operator_proof` (kdf name, cost settings, salt and digest; no secret);
  - `limit_change_pending` (empty, or the pending step-3 change: id, phase, the old and new fingerprints, the new limits, started at);
  - `admission_paused_for` (empty, or the pending change id).
- **Table `limit_changes`,** append-only: seq, at, kind (`installed`, `lowered`, `raised`, `authorised`, `aborted`, `proof-set`, `proof-changed`, `proof-refused`), old and new working values, old and new authorised values, old and new fingerprints, proof given yes/no, reason, declared account budget, declared other machines, donor receipt, and the change's own receipt.
- **Table `canary_history`:** every canary record that step 3 moved aside, exactly as it was, with the fingerprint it was taken under.

**`_verify_metadata` gains checks** that fail closed:
- working <= authorised, and soft < cutoff for the working values;
- `limits_version` equals the newest history row;
- the working values equal that row's new values;
- the proof record, when present, is well formed;
- a pending change is well formed and names the current fingerprints.

### What status and report show (new fields, additive)

- `ledger.authorised_limits`, `ledger.working_limits`: each holding the ceiling, cutoff and soft stop, in micro-EUR.
- `ledger.limits_version`, `ledger.limit_changes_recent` (the last 10 rows, without secrets), `ledger.operator_proof_set_at`, `ledger.limit_change_pending` (id and phase), `ledger.allocation_declared` (the latest declared account budget and other machines' shares).
- `worker_spend_errors` gains `limit_change_paused` while a step-3 change is pending.
- The existing fields `trial_cutoff_micro_eur`, `soft_stop_micro_eur` and `external_ceiling_micro_eur` keep their meaning: the authorised values. Programs that read them see no change; programs that want what is enforced read the working values.

## Proof plan

Every case below runs on temporary ledgers made by the code under test, in a test folder with its own local-app-data path. None of them uses the live ledger, gateway, task or ports. The gateway start and stop in step 3 use the test doubles the service tests already use.

1. **A stale repeated command:**
   - steps 1, 2 and 3 each run twice with the same `--expect`; the second run is refused and changes nothing (the database bytes compared);
   - a command prepared before another change is refused.
2. **A failed donor reduction:**
   - the donor's lower is refused (below commitments), so no receipt is printed, and the recipient's raise without a receipt is refused;
   - a raise with an `--account-raised` statement and no receipt is allowed and recorded as new money.
3. **An interruption at each durable write:** a fault is injected after each of P2, P4, P5, P6, P7, P8, P9 and P10, and inside the step-1 and step-2 transactions. For each:
   - the status line is the one in table B;
   - no call is admitted in that state;
   - resume finishes the change;
   - abort restores the old limits byte for byte (except the history rows);
   - no hold was cleared.
4. **An unsafe decrease:**
   - a cutoff below this month's commitments, and a ceiling below total commitments, are refused, with the ledger byte-identical;
   - the same request on a held ledger is refused and the hold stays.
5. **A raise without the proof:** a missing passphrase, a wrong one, one given as an argument or through the environment, input that is not a console, and a proof that was never set: each is refused, and the failed attempts are recorded. After five failures the lockout applies.
6. **The integrity fence:**
   - agenttalk 0.97.0 refuses a ledger after `limits-install` (an old-version test, as for `binding-install`);
   - a hand-edited working value above the authorised one makes the ledger refuse to open.
7. **Canary history and the one-call slot:**
   - after step 3, the old canary sits in `canary_history` exactly as before; the current canary is absent until P10, and the ledger never accepts the old one as current;
   - while the slot is open, a call without the one-time value is refused; the slot admits one reservation and then closes, even when that call fails;
   - no call other than the canary is admitted between P2 and P11.
8. **Admitted calls:**
   - a call admitted before a step-1 lower settles normally, and its overrun is recorded in full and holds as today;
   - step 3 waits for an admitted call and times out cleanly when the call never settles.
9. **Two machines:** two temporary ledgers stand in for the desktop and the VM. A donor-first move is recorded on both. The status shows "allocation declared by the operator", and nothing claims a global check.

## Sizes

Agent-days of build before review, with the tests above. Review rounds come on top: recent gateway work took two or three rounds.

| Work order | Contents | Size | Reviews | Deploy |
| --- | --- | --- | --- | --- |
| 1 | `limits-install` (schema fence), working limits, `limits` and `limits lower`, the change history, the status fields, the `_verify_metadata` checks, tests 1, 3 (steps 1 and 2 part), 4, 6 and 8, docs | M: 2-3 days | security read; cross-vendor read | one gateway window per machine, once (the `limits-install` step) |
| 2 | the operator passphrase (`set`, `change`, check, lockout), `limits raise` up to the authorised values, the allocation declaration, tests 2 (part), 5 and 9 (part), docs; optionally clear-hold needing the proof | M: 1.5-2 days | the main security read; cross-vendor read | none |
| 3 | `limits change` (P0-P11), `resume` and `abort`, the canary history, the one-call canary slot (a change in the gateway's request path as well as the ledger), the ceiling statement, donor receipts, tests 1-9 in full, docs | L: 5-7 days | security read; cross-vendor read; a rehearsal on a scratch gateway | none beyond running it |
| 4 (optional) | let a canary carry over when only the limits changed, not the prices | M: 1-2 days | security read | none |

Work orders 1 and 2 together, about 3.5-5 days, cover every change within what was authorised. That includes raises up to tomorrow's authorised 212 EUR on the desktop. Work order 3 is needed only when the authorised limits themselves must move without a re-init. The challengers' "2-5 days" fits work orders 1 and 2, not 3.

## Until this ships: the re-init scripts as the interim tool

**Recommendation: yes,** with a small change. The guided re-init of 2026-10-07 is a set of operator scripts, one per step, with a backup, a rename-aside, init, task-install, start, a canary and a rollback for every failure point. It should become the interim way to change limits until work order 3 ships, but only after tomorrow's live run and its review prove it.

It keeps today's authority model unchanged: only the operator, at the machine, in a window. It also needs no new code in agenttalk.

**What it takes,** about half a day plus one review read:
1. **Parameterise the numbers.** `reinit-numbers.py` fixes the total budget at 300 EUR, the margin at 5, the soft-stop gap at 10, the smallest allowed cutoff at 20 and the VM ceiling at 40. These become parameters that the operator passes and confirms, and that `numbers.json` and the opening evidence record. A second mode takes the ceiling, cutoff and soft stop directly, still checked against init's own rule.
2. **Parameterise the run's name.** The backup folder and the ".reinit-300-old" names come from one `-Tag`. A tag already in use is refused, so a later run never mixes with an earlier one.
3. **Parameterise the runtime.** The runtime path and its expected version become parameters, checked before step 0.
4. **Rerun both offline rehearsals** with two different tags, and run one live window before calling the tool reusable.

**What it still costs, and what steps 1-3 remove:**
- the 15-20 minute window;
- a new ledger each time, with the history kept only in the backup;
- a new canary call;
- a restart of the paid seats, because init makes new tokens.

## Open questions

1. **A working value for the ceiling.** The lead decided that ceiling changes come second, so in this design lowering the ceiling goes through step 3. Lowering a working ceiling is exactly as safe as lowering the cutoff. Adding it to step 1 would make the donor side of "move budget to the other machine" instant, with no window, and costs little extra (about half a day in work order 1). Recommendation: include it. Lead's decision.
2. **Clearing a hold.** Today `gateway clear-hold` needs no proof, and clearing a hold loosens spending. Recommendation: require the operator's passphrase once it exists (work order 2), keeping today's reason and unresolved-calls rules. Lead's decision.
3. **The per-turn cap.** Init sets the per-turn cap equal to the cutoff (44 EUR on the desktop today) and fingerprints it separately. This design leaves it alone, so after a cutoff raise one turn can still spend at most the old per-turn amount. Is that wanted, or should step 3 also move it? Moving it adds the per-turn fingerprint to table A (the ledger and the runtime marker). Recommendation: leave it for now.
4. **The canary after step 3.** One fresh paid check call per authorised change (about one cent, through the one-call slot), or work order 4's carry-over rule, which would also make the slot unnecessary. Recommendation: the fresh call; the carry-over only if changes become frequent.
5. **Who sets the passphrase, and when.** Recommendation: the operator, in the `limits-install` window on each machine, checking the status line afterwards.

## Technical notes

Code at master 33b5f9aba4dfd7df4275e3838b1f39254c1ad4e5. The gateway modules are byte-identical to v0.97.0 (3adc32a45445e84c04432c3d9f92622a1a171e08), which runs live.

- **The price policy and its fingerprint:**
  - `price_policy()` and `price_policy_hash()` include `trial_cutoff_micro_eur`, `soft_stop_micro_eur` and `external_ceiling_micro_eur` (`src/agenttalk/ovh_gateway.py` 271-332);
  - `child_cap_policy()` includes `max_micro_eur`, the per-turn cap that init sets to the cutoff by default (335-373; the default in `initialize`, 706-707).
- **The checks on every ledger open,** in `_verify_metadata` (1169-1321):
  - it recomputes the price fingerprint from the stored limits and requires both the metadata copy and the install marker copy to equal it (1210-1225);
  - it requires opening + cutoff + one reservation <= ceiling (1236-1243);
  - it requires the accepted canary's call to have been settled under the recomputed fingerprint (1308-1320). This last check is why a fingerprint change must move the canary aside.
- **Admission:** `_reserve_locked` (2715-2798) reads the cutoff and ceiling from metadata inside the reservation's own `BEGIN IMMEDIATE` transaction (`_begin`, 1889). A working limit written in its own transaction therefore applies from the next reservation, with no race.
- **Overruns:** `settle` (2998-3079) records the full actual charge even when it exceeds the reservation, marks the call uncertain and sets the durable hold "attempt <id> exceeded the reserved policy" (3062-3068).
- **Holds:** `place_hold` and `clear_hold` (3170-3215) take no credential. `clear_hold` refuses only while calls are unresolved. `gateway hold` and `gateway clear-hold` already write the ledger from the command line while the gateway runs (`src/agenttalk/cli.py` 10065-10068); steps 1 and 2 do the same.
- **The canary gates a seat's start, not admission:** the ledger status lists `dashboard_canary_absent` among `worker_spend_errors` (3759-3762), and `wrap` refuses to start on it (`src/agenttalk/cli.py` 12899-12918). `_reserve_locked` does not look at the canary, which is why step 3 keeps its pause on until P10 and admits only the canary call.
- **The operator credential today:** `_operator_credential()` reads the front token (3870-3872). `wrap` puts the same token into every paid seat's environment as its login key (`src/agenttalk/cli.py` 12946-12953).
- **The other fingerprint places:**
  - the install manifest is checked against the ledger at load (`load_install_manifest` and `_validate_install_manifest`, `src/agenttalk/ovh_gateway_service.py` 1266 and 1189-1198);
  - the task identity carries it (`_task_identity`, 248-271), and `gateway status` requires the saved identity to equal it (1977-1984);
  - the runtime marker is written by `run_service` at start with the ledger's fingerprints (1738-1756) and compared by `_runtime_projection` (1481-1532).
- **Patterns to reuse:**
  - `install_child_cap_binding` (`ovh_gateway.py` 1709-1757) commits the database first and moves the install marker second. Every reader refuses the half-way pair, and a repeat finishes it. Table B's P5 and P6 follow the same pattern, with a pending record that names the pair;
  - `reconfigure_endpoint` (`ovh_gateway_service.py` 1040-1159) rewrites the manifest of a stopped gateway without touching the ledger;
  - the schema fence: `_marker` accepts only ledger schemas 2 and 3 (`ovh_gateway.py` 1089-1111), so a ledger whose install marker says 4 is refused by 0.97.0 and older.
- **The manual re-init this replaces:** `docs/STEP-ENVELOPE-SERVICE-READERS.md`, "Upgrade path for a 0.91.0 install at a non-default envelope".
