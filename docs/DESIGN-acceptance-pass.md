# Acceptance pass design

**Audience:** agenttalk contributors and project leads designing a final acceptance check for major work. This note proposes a capability; the commands and records below are not implemented.

## Problem and boundary

Internal tests and reviews can agree with each other and still miss a defect in a plain build, an independently exercised interface, or the test harness itself. A hand-run pass on a legacy Java/AngularJS migration exposed all three kinds of gap. Agenttalk should make the final check reproducible, independently observed, and auditable for any project.

An acceptance pass runs against one frozen, clean revision after the normal implementation, CI, and review gates. It does not replace those gates, grant deployment authority, or claim that a scanner proves correctness. It verifies a declared behavioral bar and runs independent tools with explicit baselines, then asks a reviewer who has not seen the expected results to challenge the outcome.

## Concepts and records

| Concept | Proposed meaning |
| --- | --- |
| Acceptance revision | The full commit SHA frozen by `close open`; all runs, baselines, and reviewer evidence bind to it. Changing the SHA invalidates the pass. |
| Acceptance bar | A versioned list of behavioral items. Each item specifies a runnable command or observation, expected outcome, evidence artifact, owner, and whether it gates GO. A plain build and test command belong here even when CI already ran them. |
| Tool registry entry | Tool name and stack, independent property checked, exact version, source/provenance and SHA-256 of the staged artifact, offline acquisition instructions, command template, result parser, gating or informational policy, and baseline rule. |
| Cold sweep | A reviewer from a different vendor or team receives only the SHA, scope, and safety guardrails. The acceptance brief, expected counts, and first runner's results are withheld until the reviewer records independent observations. |
| Residual | A bounded finding with owner, impact, evidence, disposition, follow-up and blocking status. An unresolved blocker cannot be relabeled as an informational observation. |
| Acceptance record | One machine-readable pass record binding the close ID, revision, plan/registry hashes, bar rows, tool rows, cold-sweep pointer, residuals, and computed HOLD/GO inputs. Human-readable reports may link to it. |

Each row records the exact executed command, exit status, tool version, timestamp, artifact digest and path, expected and observed values, baseline revision and value, comparison rule, and verdict. The record distinguishes `pass`, `fail`, `not-run`, and `informational`; a missing artifact is `not-run`, never `pass`. It also records the measured scope, such as operations covered or files scanned, so an unchanged count from a narrowed run cannot pass unnoticed.

## Fit with existing agenttalk machinery

**Recommendation:** extend `agenttalk close`, rather than create a second final-verdict command. `close open` already freezes a full SHA, binds required lenses to authorized actors, and stales acknowledgments when the revision changes. `close check` already combines lenses and gates into HOLD/GO; `close publish` records the lead's terminal decision. The existing `assurance-scan` remains an evidence producer, and its artifacts may supply tool rows, but it does not approve the pass.

Proposed additions are a project-owned acceptance plan and tool registry, a validated acceptance record, and an acceptance evaluator invoked by `close check`. A possible CLI shape is `close open ... --acceptance-plan PATH`, followed by `close acceptance record --id ID --file PATH`; these names are design sketches. Opening freezes the plan and registry hashes. The evaluator checks row completeness, revision binding, baseline rules, and cold-sweep presence before it allows GO. The integrator and cold reviewer acknowledge separate required `acceptance-run` and `acceptance-cold` lenses with typed evidence pointers. The lead drafts and publishes the same close already used for milestones.

The acceptance evaluator should emit an automation-attested, revision-bound blocker gate for the declared acceptance scope. This matters because current `gate set` accepts a blocker gate as green only with `automation_ci` evidence; a seat's `local_command` claim cannot turn it green. The runner may execute locally and offline, but a trusted project automation adapter must validate and attest its artifacts before setting that gate. Until that adapter exists, the close remains HOLD for gating acceptance; an operator may use the existing explicit, expiring waiver path. Accepted blocker remediations continue to reference named gates, as `close` already requires. The acceptance record points to evidence; it does not copy logs into the close JSON.

Rejected alternative: a separate `agenttalk accept` command with its own GO/HOLD lifecycle. That would duplicate revision staleness, gate checks, counter resolution, publishing, and barrier behavior, allowing two competing final answers for the same work. A thin `accept` alias could be considered later for ergonomics, backed by the same close record.

## Project tool registry and offline use

A project declares stack profiles and selected tools in a versioned `.agenttalk/acceptance-tools.json` (proposed path). A plan selects specific registry entry IDs and marks each gating or informational before the revision is frozen. Selection is risk-based; no default tool list is silently required for every project. Typical **examples**, subject to project validation and pinned versions, are:

| Stack | Independent free-tool examples | Possible signal |
| --- | --- | --- |
| Java | SpotBugs with FindSecBugs, PMD, Checkstyle, `jdeps`, PIT | Bytecode defects, source rules, JDK dependency use, mutation resistance |
| JS/TS | ESLint, TypeScript compiler, Knip, Playwright | Static errors, unused exports/dependencies, browser behavior |
| Python | Ruff, mypy, Bandit, pip-audit, Hypothesis | Lint/type/security findings, dependency advisories, property-based behavior |
| HTTP API, if present | Schemathesis | Live contract and schema conformance |

The project chooses tools that add a signal independent of its own tests. A tool already used in CI can still run in the final pass if it is run fresh on the frozen SHA and its scope is stated; merely citing an old CI badge is insufficient. Proprietary or unavailable tools are optional, never silently substituted.

The lead or operator acquires distributions, plugins, language packages, and advisory data into a project-local cache before dispatch. They record source URL or package coordinates, version, license if relevant, retrieval time, SHA-256, and the checksum's trusted source. A checksum copied only from the same untrusted download page is provenance, not independent verification; where possible verify a vendor signature or independently published digest. Registry commands reference cache-relative paths and enforce offline package-manager flags or network denial. Seats receive read-only access to the staged cache and run with network access disabled; **no seat downloads during acceptance**. A missing artifact, checksum mismatch, stale advisory snapshot under a declared freshness rule, or attempted network fetch produces `not-run` or `fail` evidence according to the predeclared policy, never an unrecorded skip.

## Flow

1. **Lead defines the pass.** Choose the milestone scope, acceptance bar, tool entries, baseline revision and rule per row, gating/informational classification, independent reviewer, and residual owners. Write the versioned acceptance plan and tool registry; open a close on the intended SHA with `acceptance-run` and `acceptance-cold` as required lenses. Records: plan, registry, close record with plan/registry hashes.
2. **Operator stages tools.** Download approved packages and any data snapshots into the local cache, verify digests/signatures, and hand the seats a cache manifest. Records: acquisition manifest with provenance, hashes, versions, and cache-relative locations.
3. **Integrator seat runs the bar.** Use a clean checkout at the frozen SHA and isolated scratch/services. Execute each behavioral item and selected tool offline, capture the exact command and raw evidence, compare each category against its named baseline, and check actual coverage/scope. Records: signed or automation-attested run artifact, row results, evidence manifest and digests, proposed residuals. A failed plain build remains a failed bar row even if a specialized test profile passes.
4. **Cold reviewer seat sweeps independently.** Receive only SHA, scope, and safety guardrails; choose checks without the plan or first results, record commands, observations, and potential blind spots, then unblind for reconciliation. Records: time-stamped cold-sweep report and typed `acceptance-cold` lens acknowledgment or counter. The reviewer does not edit the integrator's results.
5. **Lead reconciles and closes.** Compare both accounts, route defects to owners, require a new revision and fresh pass for fixes, and bind accepted residuals to follow-up records. Record the integrator's `acceptance-run` acknowledgment, resolve counters and blocker gates, run `close check`, draft the conclusion, and publish HOLD or GO. Records: final acceptance record, residual ledger, close draft, and published close snapshot. A GO barrier is a separate deliberate release action.

## Failure and baseline rules

- A gating bar row fails on an unmet outcome, missing or stale evidence, wrong SHA, narrowed scope, or an unrun required command. A gating tool fails if its declared comparator fails, the tool cannot execute, or a new finding category appears without disposition. An informational row records the same facts but does not itself block GO; its classification cannot change after the plan is frozen without reopening.
- Baselines compare like with like: same tool and rule versions, category definition, target scope, and counting method. Report current and baseline numbers per category. A red cell is called **pre-existing** only when the baseline revision and its exact number are quoted and the current observation is comparable. An overall total cannot hide a new category or a category regression. A category increase needs a named cause and either remediation or an explicit, reviewed baseline movement; explanation alone does not make a failing comparator green.
- A first live run has no measured baseline. It establishes one after review; it cannot retroactively claim that fresh failures were pre-existing. A comparison that checks two committed baseline files against each other does not substitute for comparing fresh output against the baseline. A passing tool whose configured target excludes the changed area proves only the configured scope; record the coverage gap as a residual.
- `close check` holds on any gating failure, missing cold sweep, unexplained category movement, unresolved reviewer counter, open blocker remediation, or blocker gate that is red/unknown. The lead may publish HOLD with owners and next actions. GO requires every gating item satisfied on the frozen SHA, the cold sweep reconciled, and any allowed nonblocking residual documented with owner, impact, and follow-up. Operator waivers remain explicit and auditable under existing gate policy.
- A fix creates a new SHA. Reopen the close, invalidate prior acceptance rows and lens acknowledgments, and rerun affected checks plus any check whose comparability may have changed. Retain the earlier HOLD record and evidence for the audit trail.

## Shippable increments

1. **Manual template and close binding.** Add a documented acceptance plan and human-readable record template using existing close lenses and gates. Demo: one frozen SHA, two lens acknowledgments, and a published HOLD with evidence pointers.
2. **Registry validation and offline preflight.** Validate schema, tool pins, digests, cache presence, and command placeholders without executing tools. Demo: a valid staged tool passes preflight; a changed byte or missing package fails.
3. **Run artifact and comparison evaluator.** Capture exact commands, outputs and scope; compare category baselines and emit a machine-readable acceptance record with a trusted automation adapter for the blocker gate. Demo: a new category or narrower scope yields HOLD even when the overall count improves.
4. **Cold-sweep and close integration.** Enforce withheld brief delivery, independent reviewer evidence, unblinding/reconciliation, and revision staleness in `close check`. Demo: GO on a complete frozen pass, then HOLD after reopening at a new SHA.

## Questions for the design review

Codex Astra and a Sonnet reviewer should challenge these choices:

1. What is the smallest trustworthy automation boundary that can attest offline seat runs as `automation_ci` gate evidence without letting a seat self-certify a blocker as green?
2. Should the acceptance plan and registry hashes live in the close record, an acceptance sidecar, or both, given the close's generation and instance checks?
3. How much of a pass must rerun after a revision change to preserve independence without making a large tool matrix impractical?
4. What evidence can prove the cold reviewer received no expectations before submitting its first observations, beyond a recorded delivery and unblinding sequence?
5. Should a project be allowed to mark an unavailable tool informational after it was initially gating, and if so which operator decision and reopened record make that change visible?
