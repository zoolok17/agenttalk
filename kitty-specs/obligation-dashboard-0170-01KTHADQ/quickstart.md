# Quickstart — Obligation Dashboard (0.17.0)

Validation walkthrough for reviewers and the fresh-eyes pass. Windows
PowerShell syntax; adjust paths for your machine.

## 1. Two-root happy path

```powershell
# Two project stores with traffic
mkdir D:\tmp\dash-a; cd D:\tmp\dash-a
agenttalk init --agents claude,codex --path .
agenttalk send --from claude --to codex --kind review-request `
  --subject "review X" --meta request_id=q1 --meta mission=demo --meta wp_id=WP01 -m "please review"

mkdir D:\tmp\dash-b; cd D:\tmp\dash-b
agenttalk init --agents lead,dev --path .
agenttalk send --from lead --to dev --kind question `
  --subject "estimate?" --meta request_id=q2 -m "how long?"

# One dashboard for both
agenttalk dashboard --store D:\tmp\dash-a --store D:\tmp\dash-b --port 8770
```

Open the printed URL (`http://127.0.0.1:8770/dashboard`). Expect: two root
panels in supplied order; dash-a shows `codex` owes `reply` on "review X"
with mission/WP tag `demo/WP01`; dash-b shows `dev` owes `reply` on
"estimate?".

## 2. Live refresh (≤3 s, no manual reload)

With the dashboard open, in another window:

```powershell
cd D:\tmp\dash-a
agenttalk reply --from codex --to-request q1 --kind review-result --meta status=approved -m "lgtm"
```

Within ~3 seconds the "review X" row flips: `claude` now owes `read-reply`.

## 3. /api/state for automation

```powershell
Invoke-RestMethod http://127.0.0.1:8770/api/state | ConvertTo-Json -Depth 8
```

Expect `schema_version 1`, `roots` array of 2, no `body` keys anywhere,
`errors: []` on both roots.

## 4. Corrupt-root isolation (FR-005)

```powershell
Set-Content D:\tmp\dash-b\.agenttalk\config.json "{not json"
Invoke-RestMethod http://127.0.0.1:8770/api/state
```

Expect HTTP 200; dash-b's root object has a non-empty `errors` and omits
data fields; dash-a unaffected. Restore the config and the panel recovers on
the next poll without restarting the server.

## 5. Read-only proof (NFR-001 — also automated)

Hash every file under both `.agenttalk/` trees, browse the dashboard hard
(state polls, message details, a POST), re-hash: identical.

## 6. Bind failure (FR-010)

```powershell
# with something already on 8770 (e.g. the dashboard above still running)
agenttalk serve --port 8770; $LASTEXITCODE   # expect 2 + actionable message
agenttalk dashboard --port 0                  # expect: works, prints ephemeral URL
```

## 7. Compatibility spot-checks

```powershell
agenttalk serve --port 8771   # plain serve: prints "/" URL, single root, behaves as 0.16.0
# /api/status and /api/messages responses match pre-0.17.0 shapes
```

## 8. Loopback wall

From another machine (or any non-loopback source): connection refused /
403 — and `agenttalk dashboard --help` shows no host option at all.
