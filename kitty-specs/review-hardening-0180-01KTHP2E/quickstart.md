# Quickstart — Review Hardening (0.18.0)

Per-finding validation (PowerShell). Each maps to a spec scenario + the
reviewer repro.

## 1. Poison signature no longer DoSes the bus (FR-001/002)

```powershell
$env:AGENTTALK_HMAC_KEY_FILE = "D:\tmp\rh\k.key"
mkdir D:\tmp\rh; cd D:\tmp\rh
agenttalk init --agents alpha,beta --path .
agenttalk hmac-init
agenttalk send --from alpha --to beta -m "legit"
# drop a poison file: valid version/alg, signature is a JSON list
# (hand-write into .agenttalk\messages\…-PSNx.json with meta.signature=[1,2,3],
#  signature_version="v1", signature_alg="hmac-sha256")
agenttalk status            # expect: degrades, lists the poison id as invalid (NOT a traceback)
agenttalk recv --for beta   # expect: 'legit' delivered, poison skipped
agenttalk prune --invalid --dry-run   # expect: poison file selected
```

## 2. Malformed id can't poison the cursor (FR-003)

```powershell
# hand-write .agenttalk\messages\zzzz.json (roster-valid, id="zzzz")
agenttalk status            # expect: 'zzzz' listed INVALID
agenttalk recv --for beta   # expect: 'zzzz' NOT delivered (cursor untouched)
```

## 3. Retired history stays visible (FR-004)

```powershell
agenttalk roster retire beta
agenttalk tail --from-start  # expect: beta's historical messages PRINT (not TAIL INVALID)
# dashboard:
agenttalk serve --port 8780  # /api/messages now includes beta's history
```

## 4. Broadcast resume completes past a retired recipient (FR-005)

```powershell
# given a partial broadcast bid with audience_resolved=x,y and only x delivered
agenttalk roster retire y
agenttalk broadcast --from lead --resume <bid>
# expect: dropped=[y], remaining active copies sent, exit 0 (not perpetual 5)
$LASTEXITCODE
```

## 5. No obligation points at a tombstone (FR-006)

```powershell
# lead broadcasts a question to lead,a,b; a replies; retire b; lead consumes a's reply
agenttalk threads --for lead --json
# expect: pending excludes b; audience_retired=["b"]; next_owner never names b
```

## 6. Duplicate same-agent activation warning (FR-007/008)

```powershell
# Terminal A:
agenttalk wait --for claude --timeout 0
# Terminal B (same store, while A still waits):
agenttalk wait --for claude --timeout 5
# expect (B): "warning: another live process (PID …) is already waiting as
#             'claude' …" then normal wait behavior; exit code unchanged.
# Kill A, then start B again -> NO warning (stale/dead marker).
agenttalk doctor   # reports the current waiter pid + liveness (advisory)
```

## 7. Docs honesty (C-006/C-008)

README + SECURITY state: one window per agent (concurrent same-agent
unsupported, warned-not-enforced), and the synced-multi-machine clock-agreement
constraint (id-shape validation does not fix skew).

## 8. Back-compat + full gate (NFR-001/002)

```powershell
agenttalk --version          # 0.18.0
python -m pytest -q          # full suite green; pre-0.18 marker/heartbeat formats still read
```
