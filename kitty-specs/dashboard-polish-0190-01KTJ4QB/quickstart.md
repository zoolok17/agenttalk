# Quickstart — Dashboard Polish (0.19.0)

Validation walkthrough (PowerShell). Maps to spec scenarios.

## 1. Stats + edges on `/api/state` (FR-001/002/003)

```powershell
mkdir D:\tmp\dp; cd D:\tmp\dp
agenttalk init --agents lead,dev-a,dev-b,rev-a --path .
agenttalk roster set-role lead lead
agenttalk roster set-role dev-a developer
agenttalk roster set-role rev-a reviewer
agenttalk roster set-operator-facing lead
agenttalk send --from lead --to dev-a -m "do X"
agenttalk send --from dev-a --to lead -m "done"
agenttalk dashboard --port 8790
Invoke-RestMethod http://127.0.0.1:8790/api/state | ConvertTo-Json -Depth 8
```
Expect: `schema_version 1`; each agent has `sent`/`received`; the root has an
`edges` array (`lead→dev-a`, `dev-a→lead`), sorted by count; no
`edges_truncated` (few pairs).

## 2. Hierarchical roster + cards (FR-004/005/006)

Open `http://127.0.0.1:8790/dashboard`. Expect: `lead` (operator-facing)
centered on top; `dev-a`/`dev-b` cards in the left column; `rev-a` in the
right; any unroled agent center. Each card shows name, role/groups, last-seen,
sent, received, owes (open threads it owns), and a composing badge while
drafting.

## 3. Conversation panel (FR-007)

Below the roster, a who-talks-to-whom panel lists `lead → dev-a (1)`,
`dev-a → lead (1)`, etc. With >50 distinct pairs it shows "top 50 of N".

## 4. Refresh controls (FR-008)

- Uncheck **auto-refresh** → the page holds still (no updates, no reload).
- In another window `agenttalk send --from lead --to rev-a -m hi`, then click
  **Refresh** → the new traffic/edge appears; scroll position unchanged.
- Re-check auto-refresh → updates resume (~2 s).

## 5. Back-compat + security (NFR-001, C-003/004/005)

```powershell
# /api/status and /api/messages shapes unchanged; /messages/<id> CSP identical
agenttalk --version          # 0.19.0
python -m pytest -q          # full suite green incl. no-mutation + CSP-pin
```
Read-only proven by the existing full-tree-hash regression; `/dashboard` keeps
the script-capable CSP, all other routes byte-identical; no `body` in
`/api/state`.
