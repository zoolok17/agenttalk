# Quickstart: 0.15.0 Team Scope

```powershell
mkdir scratch-0150; cd scratch-0150
agenttalk init --agents lead,rev-a,rev-b,impl-c
agenttalk roster set-role rev-a reviewer; agenttalk roster set-role rev-b reviewer
agenttalk roster set-role impl-c implementer
```

## 1. Role-scoped broadcast (S1)
```powershell
agenttalk broadcast --from lead --to-role reviewer --kind question -m "fresh eyes on WP07?"
# EXPECT: 2 copies (rev-a, rev-b); impl-c sees nothing (threads --for impl-c empty)
agenttalk broadcast --from lead --to-role ghost -m x; $LASTEXITCODE   # EXPECT 2 + known roles named
agenttalk roster set-role rev-b implementer
agenttalk threads --for lead   # EXPECT: pending still [rev-a, rev-b] — frozen, no drift
```

## 2. Not-applicable reply (S2)
```powershell
agenttalk reply --from rev-b --to-request <BID> --na
agenttalk threads --for lead   # EXPECT: rev-b responded, marked (n/a); rev-a still pending
# refusal: reply --na on a review-request thread -> exit 2
```

## 3. Delivery accounting (S3)
```powershell
# fault injection is test-only; manual check: every copy carries batch_total
agenttalk status   # EXPECT: incomplete-batch warning ONLY if copies < batch_total
```

## 4. Quarantine (S4)
```powershell
# hand-drop a bogus file into .agenttalk/messages/ (unknown kind)
agenttalk status | Select-String INVALID          # EXPECT: 1 invalid
agenttalk prune --invalid --dry-run               # EXPECT: lists it, moves nothing
agenttalk prune --invalid                         # EXPECT: moved to .agenttalk/quarantine/
agenttalk status                                  # EXPECT: 0 invalid, quarantined=1
```

## 5. Gate
```powershell
pytest -q          # full suite green
# release: CI matrix green (gh run watch) BEFORE tagging — NFR-005
```
