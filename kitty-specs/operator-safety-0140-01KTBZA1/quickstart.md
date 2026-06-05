# Quickstart: 0.14.0 Operator Safety

Smoke-walk for every feature once implemented. PowerShell syntax;
run from a scratch directory. Mirrors spec.md Scenarios 1–5.

```powershell
# Setup: fresh store with a liaison and two workers
mkdir scratch-0140; cd scratch-0140
agenttalk init
agenttalk roster add lead; agenttalk roster add worker-a; agenttalk roster add worker-b
agenttalk roster set-operator-facing lead
```

## 1. Rescind + wake (Scenario 1)

```powershell
# lead opens a tracked request to worker-a (note the printed request_id)
agenttalk send --from lead --to worker-a --kind question --subject "fire?" -m "fire the launch"
# worker-a blocks on the thread in a second window:
#   agenttalk wait --for worker-a --to-request <RID> --timeout 120
agenttalk rescind --from lead --to-request <RID> -m "new data - hold"
# EXPECT: worker-a's wait wakes with a RESCINDED banner, exit code 3 (not 1, not 0)
agenttalk threads --for lead          # EXPECT: thread state closed-superseded
```

## 2. Pre-action check (Scenario 2)

```powershell
agenttalk check --for worker-a --to-request <RID>; $LASTEXITCODE   # EXPECT: superseded / 3
agenttalk check --for worker-a --to-request esc-nonexistent000; $LASTEXITCODE  # EXPECT: unknown / 4
# open a fresh request, then: check -> current / 0
```

## 3. Root hardening (Scenario 3)

```powershell
mkdir sub; cd sub
agenttalk init; $LASTEXITCODE         # EXPECT: refusal, exit 2, names ..\.agenttalk
agenttalk init --force                # EXPECT: deliberate nested store created
cd ..
agenttalk doctor                      # EXPECT: first line root:, multi-store warning naming both
$env:AGENTTALK_ROOT = (Get-Location).Path
cd sub; agenttalk whoami              # EXPECT: first line root: <parent>, not .\sub (env wins over walk)
Remove-Item -Recurse -Force sub; Remove-Item env:AGENTTALK_ROOT; cd ..
```

## 4. Escalation (Scenario 4)

```powershell
agenttalk escalate --from worker-b -m "Deploy window: today or tomorrow?"
# EXPECT: routed to lead automatically, prints esc-... request_id
agenttalk sync --for lead             # EXPECT: 'operator input needed' bucket with the question
agenttalk reply --from lead --to-request <ESC_RID> -m "Operator says: tomorrow." --meta operator_answer=true
agenttalk sync --for lead             # EXPECT: bucket empty
agenttalk threads --for worker-b      # EXPECT: escalation answered/closed
# Failure modes:
agenttalk roster set-operator-facing --clear
agenttalk escalate --from worker-b -m "ping"; $LASTEXITCODE   # EXPECT: refusal, exit 2, remediation hint
agenttalk roster set-operator-facing lead
agenttalk escalate --from lead -m "self"; $LASTEXITCODE       # EXPECT: refusal, exit 2 (liaison owns the channel)
```

## 5. Reply-in-flight (Scenario 5 — only if #14 made the release)

```powershell
# worker-a owes lead a reply on <RID2>; while drafting:
agenttalk composing --from worker-a --to lead --to-request <RID2>
agenttalk threads --for lead          # EXPECT: row annotated reply-in-flight (and no stale warning)
```

## 6. Compatibility spot-checks (NFR-001)

```powershell
pytest -q                              # full suite: pre-existing tests pass unmodified
agenttalk drain --for worker-a         # rescind messages appear as ordinary transcript content
# A 0.13.0 install pointed at this store reads/sends normally and ignores
# operator_facing / needs_operator / <agent>.composing.json.
```
