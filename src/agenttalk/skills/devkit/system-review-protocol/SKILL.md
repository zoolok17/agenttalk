---
name: system-review-protocol
description: >-
  Run a MILESTONE or FULL-REPO adversarial review and drive it to a HOLD/GO close.
  Use ONLY when explicitly asked to run a system/milestone/release review or to open
  and drive an `agenttalk close` across multiple review lenses. Do NOT use for a single
  PR/diff review (use review-code or a specific review-* lens), an implementation
  handoff (use agenttalk-handoff), team routing (use agenttalk-lead), or anything that
  is not a multi-lens close. This skill ORCHESTRATES the existing `agenttalk close`
  (P2/P3) flow — it invents no new commands.
reviewed-against: "0.42"
category: assurance
evidence-profile:
  - review-result
---

# system-review-protocol

A milestone review is wide: many surfaces, several specialist lenses, real remediation.
Its job is to gather honest typed evidence under each required lens and converge on ONE
auditable HOLD/GO via `agenttalk close` — not to re-judge code itself. Drive the
existing flow; never invent a parallel workflow or hand-wave a GO.

## SCOPE — freeze what is under review
- [ ] Open the close on a frozen revision:
      `agenttalk close open --id <id> --scope release --revision <ref>` (resolves to a
      full SHA; a dirty tree needs `--dirty-artifact`).
- [ ] Declare the required review LENSES the milestone needs. If the project ships a
      `.agenttalk/signoffs.json` risk policy, DERIVE them from the risk classes in play:
      `agenttalk close open … --derive-signoffs --risk-class <class> --changed-path <…>`
      (or `agenttalk close signoffs apply`), which routes specialists by role/group +
      domain reviewers. Otherwise declare explicit lenses at open.

## RISK (hard rule) — the risk inventory routes the specialists
- [ ] The risk inventory you derive/declare here decides which specialist sign-offs P3
      requires. List EVERY touched risk class (a milestone touching auth + storage is
      `security` AND `persistence`, not just one), not the single most obvious one —
      under-declaring silently under-routes specialists and manufactures a false GO. The
      lead-owned close risk inventory is authoritative; a lens's self-reported
      `risk_class` is only an input to it, never the decider.

## GATHER — one honest verdict per lens
- [ ] Solicit each lens as a review/tester HANDOFF (use the matching skill:
      review-failure-injection, review-contract-drift, review-release-readiness,
      tester-qa, review-code/-docs). Each produces a `kind=review-result` with typed
      evidence + `risk_class`.
- [ ] Record each terminal verdict on the close:
      `agenttalk close ack --id <id> --lens <lens-or-generated-signoff-id>
      --status accept|counter|na --from <agent> …` carrying the typed evidence.
- [ ] A reviewer raising a **COUNTER** stays unresolved until the lead decides it:
      - reject: `agenttalk close counter decide --id <id> --counter <cid>
        --decision reject --reason <…>`.
      - accept: `agenttalk close counter decide --id <id> --counter <cid>
        --decision accept --reason <…> --rem-owner <who> --rem-fix <what>
        --rem-verification <how>` — accepting RECORDS a remediation item, so the
        remediation fields are required. For a release blocker add `--blocker --gate
        <gate-id>`; GO then needs that gate green from `automation_ci` or a waiver.

## CONVERGE — draft, check, publish
- [ ] Draft the merged conclusion: `agenttalk close draft --id <id> --from <lead> -m <…>`.
- [ ] `agenttalk close check --id <id>` — read the HOLD codes; exit 0=GO, 3=HOLD. Do NOT
      publish GO while it is HOLD. Resolve every code (missing/unauthorized/stale lens,
      undecided counter, open blocker remediation, missing/unroutable/stale signoff,
      gate HOLD) at its source — never by hand-waving.
- [ ] Publish only when check is GO: `agenttalk close publish --id <id> --from <lead>
      --verdict go` (add `--bump-barrier` only as the deliberate release act). A HOLD is
      published as `--verdict hold` and never bumps the barrier.
- [ ] The lead-only escape for an unroutable/unavailable specialist is
      `agenttalk close signoffs override --id <close-id> --set <set-id> --from <lead>
      --reason <…>` — recorded and audited, NOT counted as a real sign-off. Use sparingly.

## HONESTY (hard rule)
- [ ] The close aggregates CLAIMS into a verdict; it does not verify them. Treat a GO as
      only as trustworthy as its evidence: release-blocking lenses must anchor to
      `automation_ci` gates, and `tests_executed` must be real (command + result/exit or
      a CI run id), never fabricated. If a lens cannot honestly ACCEPT, it COUNTERs or
      NAs — a milestone close is exactly where a false GO does the most damage.
