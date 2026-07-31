# 87-A delta panel — consolidated obligations (round 3)

> Review record for Design 87-A, panel over `f42570d..44b3787` on branch
> `design/87a-classifier-authority-revision-codex4`. Tracked in-repo deliberately: this is the
> authoritative brief for the fold, and a 17 KB bus message was retried 20 times against a provider
> quota wall on 2026-07-30, which is pure waste. Agents should read this file and reply on the bus.
87-A DELTA PANEL RESULT: NOT APPROVABLE. 1 APPROVE / 2 REVISE. Four blockers, four majors.

Panel over f42570d..44b3787, three rotated lenses. Verdicts:

  Lens 1  split integrity      claude-agenttalk-reviewer-3      APPROVE  (1 minor)
  Lens 2  authority safety     codex-agenttalk-reviewer-1       REVISE   (4 BLOCKERS, 2 majors), release_blocker=true
  Lens 3  recompute + contract claude-agenttalk-reviewer-fable  REVISE   (2 majors, 3 minors)

Full artifacts are on the bus; this is the consolidated obligation set. Producer-check every premise
before folding - two of these are premise-level claims about SHIPPED code, and if one does not hold I
want the refutation with the code rather than a fold.

WHAT SURVIVED, so you do not re-litigate it: the banked evidence is genuinely intact. Lens 1 traced all
~14 deleted/rewritten classifier regions and found a home for every one, including the specific
"backoff" word-drop and the KILL_SWITCH_ACTIVE "every"->"every new" qualifier - it called both correct
narrowings rather than silent deletions. Lens 3 independently reproduced both banked
RecoveryConditionFingerprintV1 vectors byte-exact from the head payload bytes, and confirmed ZERO
matrix rows touched. Both lenses independently recomputed the frozen size disclosure and found it
correct to the byte. The 48-row/96-cell matrix is not in scope for the fold.

============================ BLOCKERS (Lens 2) ============================

B1. TWO UNTIMED ABSENCES CAN AUTHORIZE KILLING A WRAPPER THAT IS LEGITIMATELY STARTING ITS CHILD.
    Module lines 39-49, 404-422, 439-480, 626-663.
    The proof establishes two adjacent current complete ABSENT captures but never establishes that the
    child-establishment window has CLOSED. Adjacency is sequence ordering, not dwell. A live forking
    launcher with unchanged turn/progress basis yields two valid absences; action-time closure then
    reconfirms absence, prevents the child appearing, and permits Stop-Tree.
    This is grounded in shipped code, not hypothesis: wrapper/run.py:1502-1523,1983-1987 writes ACTIVE
    immediately after launcher Popen, while supervisor.py:389-390,4553-4567,4631-4658 TODAY refuses
    child-dead confirmation during a 30-second spawn/handoff grace. The new normative reducer OMITS
    that predicate. So the module's line-39 claim that the wrapper has "no progressing CLI turn" is
    stated but not enforced, and the design would REGRESS an existing shipped safety property.
    I rate this the most serious finding of the round: it authorizes killing a healthy starting agent,
    which is the exact failure the whole 87 family exists to prevent.
    Required: a typed NONRENEWABLE child-establishment grace, or a positive launch-complete/failed
    handshake bound to the same turn/launcher identity. Its expiry/result must participate in the
    ordinary confirmation basis, the reservation, and action-time equality. Pre-expiry absence must
    remain CURRENT_UNKNOWN_ACTIVE_CHILD/HOLD. Add a mandatory control for two complete PRE-HANDOFF
    absences.

B2. CAS SERIALIZES THE STATE UPDATES BUT NOTHING LINEARIZES THE EXTERNAL CALLS THEY ARM.
    Module 682-699, 982-1005, 1058-1071, 1102-1124; core 1334-1359, 1794-1799.
    Two concrete interleavings of conforming current-supervisor invocations:
    (a) P1 commits TREE_CLOSURE_ACQUIRING and pauses before #120.acquire. P2 reconciles the persisted
        phase, NEVER_ACQUIRED finalizes CLOSURE_VETOED. P1 resumes and acquires. NEVER_ACQUIRED leaves
        no terminal attempt tombstone and a late acquisition success has no mandatory stale-success
        release, so an UNOWNED CLOSURE SURVIVES.
    (b) P1 commits TEARDOWN_IN_FLIGHT plus debt, pauses before Stop-Tree. P2 sees matching HELD, treats
        it as already-issued teardown, captures the still-intact tree as SAME_OWNER_SURVIVED, releases
        and finalizes the fence - and P1 then resumes the DESTRUCTIVE CALL after authority ownership
        was released.
    ChildlessSafetyReconciliationGateV1 distinguishes only dry-run vs current supervisor; it has no
    invocation/executor owner and no proof the original continuation died. "Original live invocation"
    (module 1027-1029, 1070) is not represented in checked state.
    Required: an exclusive checked executor/continuation lease, OR separate "armed" from "externally
    issued/completed" phases with effect-linearization semantics. NEVER_ACQUIRED must terminally fence
    that attempt or late success must be obligatorily released. TEARDOWN_IN_FLIGHT reconciliation must
    not infer action completion while the issuing continuation can still run. Two-poller controls at
    BOTH commit/effect gaps.

B3. THE LOAD-BEARING THREE-WAY NONCE JOIN HAS AN UNDEFINED OPERAND. Module 262-280.
    The constructor requires `checked managed launch nonce == strict runtime launch nonce == parsed
    observed-root launch nonce`, but "strict runtime launch nonce" appears NOWHERE ELSE in either
    normative document, and OwnedWrapperIdentityV1 retains only one resulting nonce.
    I VERIFIED THIS PREMISE MYSELF against shipped code: wrapper_runtime.py RECORD_KEYS (lines 64-80)
    contains schema_version, agent, wrapper_pid, wrapper_start, wrapper_generation, phase,
    turn_generation, turn_id, message_id, cli_launcher_pid, cli_launcher_start, progress_sequence,
    last_progress_at, last_outcome, updated_at - and NO launch-nonce field of any kind. The premise
    holds exactly. A conforming implementer must either fail closed forever or silently collapse one
    supposedly independent equality, so PID-reuse safety is NOT established as specified.
    Required: define and version the strict-runtime nonce field, its producer, validation and
    migration; OR change the equality to the actual independent sources and make THAT provenance
    normative. Controls must independently remove/mismatch each required source and prove refusal.
    Operational note for context: when I terminated a wedged wrapper today I proved ownership with
    recorded wrapper_start vs live process StartTime (delta 0) plus wrapper_generation - a TWO-factor
    match, because the third factor does not exist in the record. That is adequate against PID reuse
    but it is not the three-way join this document claims. Write what is implementable.

B4. THE HARD CAP AND THE TEARDOWN DEBT CAN BOTH BE ERASED BY THE DESIGN'S OWN ALLOWED STATE-LOSS PATH.
    Core 762-764; module 72-80, 757-861.
    The core permits a new state_epoch after irrecoverable state loss. state_epoch is part of
    OwnedWrapperIdentityV1, while the attempt cycle and outstanding debt exist ONLY in the lost checked
    state. The module specifies reset/clear transitions for successful cleanup, a new guarded owner and
    authorized manual cleanup - but NO state-loss transition. So the same physical
    (pid,start,nonce,generation) incumbent becomes a "new" logical owner after epoch recreation and
    regains three automatic attempts; and state loss after a PARTIAL Stop-Tree erases the debt that is
    claimed to globally block relaunch around surviving rootless members. This contradicts the stated
    crash/reload, partial-teardown and retry-fade-out boundary at module 57-60.
    Required: a fail-closed state-loss/quarantine state denying named teardown AND every launch until
    cap/debt provenance is recovered or a provably DIFFERENT physical owner is established. Conformance
    must cover loss after attempts 1, 2 and 3, and after debt is persisted and partially acted.

============================ MAJORS ============================

M5. THE NO-PERSISTENCE-PLANE INVARIANT WAS WEAKENED, NOT PRESERVED. Core 104-110, invariant row 2543.
    TWO INDEPENDENT LENSES CONVERGED HERE, which is why it is a major and not a minor. Lens 2 calls it
    weakened; Lens 3 calls it a bounded disclosed relaxation. They agree on the FACT and differ only on
    severity. Base said "no daemon, persistence plane, or runtime dependency" and "existing files/state
    only". Head says no SEMANTIC persistence plane/package/third-party dependency, and explicitly
    pre-authorizes task #120 to disclose "a bounded OS object or bundled-helper mechanism".
    pyproject.toml:13 `dependencies = []` does remain hard in both the paragraph and the inventory row,
    so the package half is intact - but the persistence/mechanism boundary is not.
    "Semantic persistence plane" is undefined, which means the invariant's boundary becomes whatever
    #120's reviewer happens to think it is. This is exactly the surface I flagged as region (d) and put
    in scope for all three lenses.
    Required: restore the unqualified invariant, OR define the exception tightly - lifetime, ownership,
    cleanup, crash semantics, and why it is not a new persistence plane - and update the disposition
    and invariant wording so a later reviewer does not inherit the OLD guarantee. Zero runtime
    dependencies is a project-level promise; it does not get relaxed implicitly in a design doc.

M6. SPAWN_IN_FLIGHT ACCEPTS A VALID spawned_guard BUT NO NORMATIVE TRANSITION PRODUCES THAT STATE.
    Core 498-501, 1801-1804. The only displayed producer enters SPAWN_IN_FLIGHT BEFORE Start-Process,
    hence with a null guard; an ambiguous guard goes to AMBIGUOUS_LAUNCH; a valid identity commits
    straight to IDLE. Accepting a valid-guard standalone phase creates an undefined, reload-sensitive
    state implementations will disagree about.
    Required: restore the base invariant that SPAWN_IN_FLIGHT.spawned_guard is null, OR specify the
    exact producer CAS, semantics and crash/reload transition.

M7. manual_candidate SUPPRESSES A CONFIRMED-ABSENCE NO-KILL RELAUNCH, CONTRADICTING THE DELTA'S OWN
    PROSE. (Lens 3.) Classifier doc, "candidate before acknowledgement applicability" equation, diff
    hunk @1622, head ~line 1625.
    Derivation: a wrapper that crashed AFTER its CLI child died is reachable in production. Crash
    residue keeps the strict runtime record bound => dominant CURRENT_TEARDOWN_PROOF; banked child-dead
    counter reaches 2; ActiveChild ABSENT => child_death_sourced_dominant true =>
    childless_teardown_required true with TeardownDebtV1 = NONE. Presence is ABSENT with
    PhysicalAbsenceProofV1.CONFIRMED. The module's INITIAL authority requires PRESENT_TARGETABLE, so no
    named authority exists => the second branch fires => manual_candidate = SAFETY_HELD. Control never
    reaches the `RELAUNCH_ONLY if CONFIRMED` branch. At base f42570d the same evidence yielded manual
    RELAUNCH_ONLY. Meanwhile the AUTOMATIC path in that same state still relaunches (banked cell
    CURRENT_TEARDOWN_PROOF x ABSENT x STALE = RELAUNCH_ONLY).
    Net effect: the delta makes MANUAL origin strictly WEAKER than AUTOMATIC for a no-kill restart -
    inverting the manual-wins philosophy, and contradicting the delta's own statements that suppression
    covers "generic strict teardown ... before selection" (not relaunch) and that the whole-wrapper-
    absence relaunch path is not suppressed. Operator runs request-restart on a dead-wrapper-dead-child
    agent, gets refused, and watches the supervisor relaunch it automatically anyway.
    This one matters to me directly: request-restart on a genuinely absent wrapper is the ONE recovery
    action I have measured working (~22-90s), and I used it four times today. Do not break it.
    Required: split the guard. The pre-relaunch SAFETY_HELD arm must trigger on
    `teardown_debt is OUTSTANDING` (the laundering guard, which is correct), NOT on bare
    child_death_sourced_dominant. With no debt, the named-case gate should constrain only the KILL
    branch and fall through to `RELAUNCH_ONLY if CONFIRMED`.

M8. SEVEN NEW DIGEST DOMAINS, ZERO FIXED CONFORMANCE VECTORS. (Lens 3.) Module: owner_identity_id,
    OwnedTargetDigestV1, process_source_digest, owned-childless-confirmation basis, basis_id,
    authority_id, debt_id.
    The document's stated goal is that two conforming implementations choose the same values, and the
    core banks two byte-exact vectors precisely for that - a reviewer has now twice used them to confirm
    conformance. These seven get none, yet they participate in action-time EQUALITY checks where any
    byte-level disagreement (e.g. the canonical field set of CaptureIdV1 inside basis_id, or
    OwnedTreeCoverageV1 serialization) produces a permanent equality veto => permanent POLICY_HELD.
    Fail-closed, so no unsafe kill - but an availability failure invisible until integration and
    undebuggable from summaries that deliberately omit the tuples. Conformance item 10 recomputes only
    the banked CORE vectors.
    Required: bank at least one fixed byte+digest vector per new domain, or one composite chained
    vector, as the core already does.

============================ MINORS ============================

m9.  Stale single-condition restatement (Lens 1). Classifier 1778-1787: the precondition became the
     two-conjunct `recovery_execution == IDLE and recovery_poll_terminal_sequence !=
     ordinary_poll_sequence`, explicitly "the exact precondition" - but three lines later the
     manual_readiness sentence still says both values "permit a new reservation when
     recovery_execution == IDLE". Update the restatement or delete it.
m10. Label discipline (Lens 3). The module's two leading "Safety decision: ENFORCED" claims (two-capture
     teardown; never-pattern ownership) are only implementable AFTER task #120, but lack the
     "ENFORCED after task #120" qualifier the same document applies carefully elsewhere. Given this
     design's history of overclaim findings, tighten both labels.
m11. Determinism gap in reload residual captures (Lens 3). Well-formedness constrains every ordinary
     tree/debt observation to capture_ordinal == 0 at the current ordinary_poll_sequence, but the
     TEARDOWN_IN_FLIGHT reload path takes "a fresh OwnedDebtResidualObservationV1" mid-poll whose
     ordinal domain is unstated. Two implementations can disagree on whether it must be 0 (impossible
     mid-poll) or > 0; one refuses forever. State the rule explicitly, as was done for post-action
     captures.

============================ HOW TO FOLD ============================

1. Producer-check each premise FIRST. B1, B3 and M7 are derivations against shipped code or the
   equations; if any derivation is wrong, refute it with the code/equation and do not fold it. I
   verified B3's premise myself and it holds; I did not verify B1's or M7's independently, so those two
   are the ones to check hardest.
2. B1 and B4 are both "the design lost a safety property that already exists or is claimed elsewhere".
   Fix them as restored invariants with mandatory controls, not as prose caveats.
3. M5 needs a decision, not just wording: either the unqualified zero-persistence invariant stands, or
   the exception is defined precisely. Do not leave "semantic" undefined. If you think the relaxation
   is genuinely necessary for #120, say so explicitly and I will take it to the operator - that is a
   project-promise change and it is not yours or mine to make silently.
4. Produce a disposition register for all eleven items in the style of your 59-ID Revision 2 register:
   present / deferred / dropped / premise-refuted, each citing the exact location.
5. Do NOT re-open the matrix, the fingerprint vectors, or the size disclosure. All three were
   independently reverified this round and are correct.
6. Design docs only. No code, no tests, no dev-gate, no build. Read, revise, commit, report the SHA -
   I publish. The branch is now on origin as design/87a-classifier-authority-revision-codex4.

Note on scope, because it changed mid-round: I initially told the panel the previously-approved
classifier document was byte-identical and out of scope. That was WRONG - 44b3787 deleted 62 lines from
it - and I corrected it before the reviewers started. Two of the findings above (M5 and M7) live inside
that modified region. Had my original framing stood, the panel would have been structurally unable to
find either.

