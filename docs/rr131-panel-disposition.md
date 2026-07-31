# Panel disposition: supervisor observation authorization (#131)

Design under review: `docs/DESIGN-supervisor-observation-authorization.md` at `8b1b1ab`.
Three independent lenses, nine author-posed questions. **Verdict: revise + spike. Do not implement.**

| Q | Lens | Disposition | Class |
|---|---|---|---|
| 1 | A | **HOLDS** | core bypass premise survives an adversarial attempt |
| 3 | A | CANNOT-ESTABLISH | under-specified; existing helper already correct |
| 7 | A | CANNOT-ESTABLISH | deferred by design; method judged structurally sound |
| 2 | B | CANNOT-ESTABLISH | **blocked on physical evidence that this host contradicts** |
| 8 | B | **BREAKS** | real bypass; design's own invariant violated by the shim |
| 4 | C | **HOLDS** | — |
| 5 | C | **BREAKS** | migration grafts hardened evidence onto a legacy record |
| 6 | C | **BREAKS** | irreversible step before first proof; no rollback |
| 9 | C | **BREAKS** | schema cannot express the ordering its prose requires |

Two lenses returned `rejected` with release_blocker set; one returned `needs-info`.

## What survives

The central idea is sound and should not be redesigned. Q1 — can a caller satisfy both the canonical
launch grammar and the generated-shim ancestry without running `supervisor.ps1` — **held under attack**.
Q7's *method* was judged stronger than the framing I supplied: I primed lens A with today's two
CPython-grammar leaks, and it correctly refused to inherit the suspicion, because those were
*parsing heuristics* misclassifying tokens whereas this design uses byte-exact equality against a
closed set of pre-rendered templates. There is no token-classification step for an adversarial
spelling to slip through. Keep that.

## The four BREAKS, in fix order

**Q9 first — it is a specification contradiction and the cheapest to close.** The prose requires each
authority record to retain the greatest successfully published sequence so a delayed lower-sequence
success cannot roll it back. The normative schema block has `authorization_attempt_sequence` and **no
greatest-successful field**; only the instance file gets one. Transaction step 12 publishes with the
attempt sequence and never compares against the existing record, and step 5 checks only that
`allocated_through` is not *below* the attempt, which is always true and is not an ordering check. So
an implementer conforming to the schema plus step 12 produces exactly the rollback the prose forbids.
Fix the schema, not the prose.

**Q8 — a real bypass, and it is the design's own rule broken by its own shim.** The rule is that
caller-controlled values are evidence, never authority. The generated shim defaults
`AGENTTALK_PYTHON` only when absent and then *executes the inherited value as a command*
(`supervisor.py:6767-6774`), and the threat model explicitly grants the caller that variable. The
chain: set it to a prelude batch, launch the canonical absolute `supervisor.ps1`; the genuine
`agenttalk.cmd` invokes the prelude **inside the same `cmd.exe`**, so no additional process exists to
identify it. The prelude receives `%*`, can alter environment and authority arguments, then calls the
baked Python with `-m agenttalk`. Observed chain stays selected-PowerShell → system `cmd.exe` → baked
Python; provenance, artifact bytes, ancestry, image identity and start order all pass, and the record
can still say `interpreter_source=baked`.

**Q5 — the migration creates the case that breaks it.** No adversary needed. A parseable schema-1
record must parse, because the design requires distinguishing "legacy present but untrusted" from
"invalid bytes". Preconditions the documented sequence produces: the old supervisor observed a
mid-poll activation and was stopped, leaving `active=true, observations={mid_poll}` with no
`resolved_at` (step 3's switch removal is not an observer call; step 4 is a mutation). At step 5 the
switch is active, the record parses, `active` is already true and `startup` is absent — so the **fold**
branch at `supervisor_runtime.py:440-447` merges the hardened startup observation *into* the schema-1
record instead of replacing it.

**Q6 — the documented sequence can end with no supervision and no way back.** Step 4
(`--refresh-scripts`) rotates the artifact generation and old script bytes are refused thereafter,
but the first evidence that a hardened observation is even possible on this host arrives at step 5.
There is no documented rollback. Separately, step 5's "with no supervisor running" is prose, not a
checked precondition.

## The compound finding neither lens could see alone — this is the P1

Lens B **measured**, on this machine, that a same-process `Get-CimInstance Win32_Process` query for
`ProcessId`/`CreationDate`/`CommandLine` returns `CimException: Access denied` under PowerShell 7.6.3
FullLanguage, and still denied under ConstrainedLanguage. Lens C independently found that the
irreversible migration step precedes the first proof the new authority works, with no rollback.

Together: **an operator following the documented migration on a host where the provider is denied
completes steps 1–4, cannot produce a new-schema observation at step 5, and cannot restore the old
one — because the refreshed generation already invalidated the script that worked.** The end state is
"held", which means no claim, which means no supervision at all. Reached by following the
instructions correctly, on a host we have measured.

Q2 is therefore not merely "unproven". It is unproven *and* the failure mode is a self-inflicted
outage with no exit. The remedy is a read-only provider preflight that must pass **before** the
irreversible step — lens C proposed a `supervise --check-authorization-provider` shape — or a
documented rollback for a failed step 5. A design that can brick supervision by being followed is
not shippable regardless of how good its bypass resistance is.

## Q1 and Q8 are about different claims, and the design conflates them

Q1 proves the *process chain*. Q8 injects code **inside** an existing process (a nested batch in the
same `cmd.exe`), which no chain check can see. "The chain is provable" and "only supported code ran"
are separate propositions, and the design's summary reads as if the first delivers the second. The
revision must state which one it actually claims — and the honest answer is the first.

## Not defects; specify and move on

- **Q3**: `powershell_host.normalized_path_key` already does the right thing —
  `normcase(_normalize_final_path(...)).casefold()`, pure lexical normalization, no symlink/junction
  resolution, no 8.3 expansion. That is exactly the behaviour required, and a `Path.resolve()`-style
  implementation would silently defeat the junction-alias refusal. Cite the helper normatively so an
  implementer cannot pick the resolving variant.
- **Q7**: keep byte-exact closed-template equality. The residual is whether the supported emitters
  produce a stable closed set across the host matrix — that belongs in the same spike as Q2.

## Carried low-confidence finding

Lens A flagged (per the standing instruction not to drop low-confidence findings) that
`_system_cmd_path()` computes the expected `cmd.exe` from the **calling** process's bitness rather
than the observed ancestor's, so a mixed-bitness host could false-*refuse* a supported launch. This is
the same defect class dev-3 already fixed once for the observer resolver, resurfacing as a design
requirement. Fold it into the Q2 spike.

## Required next steps

1. Fix the Q9 schema contradiction — normative, no spike needed.
2. Close Q8, or restate the claim so it does not assert what Q8 breaks. Removing the inherited-value
   execution is the direct fix; if the override must stay, it cannot be executed as a command.
3. Rework the migration: provider preflight before the irreversible step, a documented rollback, and
   step 5's precondition made checked rather than prose. Fix the Q5 fold-vs-replace rule.
4. Run the Q2 spike — the provider matrix across x64, x86/WOW64 and ARM64, including the measured
   denial — and publish the evidence artifact the design already gates on. Include the
   `_system_cmd_path()` architecture question.
5. Re-panel **only** the four BREAKS plus Q2. Q1, Q4 and the Q3/Q7 methods do not need re-litigating.

PR 107 (#114) stays held throughout. Nothing here is on a release clock: v0.80.0 shipped without it.
