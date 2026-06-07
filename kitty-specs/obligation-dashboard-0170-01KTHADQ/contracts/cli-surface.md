# Contract: CLI + HTTP Surface — Obligation Dashboard (0.17.0)

## CLI

### `agenttalk serve` (UNCHANGED — contract restated for the gate)

```
agenttalk serve [--host {127.0.0.1,::1,localhost}] [--port N] [--quiet] [--access-log]
```

- Behavior, flags, startup message (`serving read-only dashboard at <url>` →
  the `/` URL), and exit codes are byte-compatible with 0.16.0, EXCEPT:
- **NEW (FR-010)**: a bind failure (`OSError`) exits **2** with an actionable
  message naming `host:port`, the likely cause (another program listening),
  and remedies (`--port 0` / another `--port`). Previously this escaped to
  the generic top-level handler.
- Serves all routes including the new ones (same server code).

### `agenttalk dashboard` (NEW)

```
agenttalk dashboard [--port N] [--access-log] [--store PATH]...
```

- Alias to the same server implementation. Binds `127.0.0.1` only — there is
  deliberately no `--host`.
- `--store PATH` repeatable; dest `stores` (NOT the global `--root` dest).
  Order is preserved into `roots[]`. No `--store` → the normally-resolved
  single root (global `--root` / upward walk, same as `serve`).
- Startup: prints the `/dashboard` URL; WARNs (stderr, non-fatal) for any
  store path lacking `.agenttalk/` at startup (D4).
- Exit codes: 0 after clean Ctrl+C; 2 usage / bind failure / store-resolution
  failure when no `--store` given (same as `serve` today); 130 never (SIGINT
  handled as clean stop, matching `serve`'s current behavior).

## HTTP routes (allowlist — anything else 404s)

| Route | Method | Status | Notes |
|---|---|---|---|
| `/` | GET/HEAD | unchanged | message-log index (root[0]); + one additive link to `/dashboard` |
| `/messages/<id>` | GET/HEAD | unchanged | detail HTML; CSP byte-identical to 0.16.0 |
| `/api/status` | GET/HEAD | unchanged | root[0]; shape pinned by existing tests |
| `/api/messages` | GET/HEAD | unchanged | root[0]; shape pinned by existing tests |
| `/api/messages/<id>` | GET/HEAD | unchanged | root[0] |
| `/favicon.ico` | GET/HEAD | unchanged | 204 |
| `/dashboard` | GET/HEAD | **new** | hierarchy HTML, all roots; CSP: `default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; img-src 'none'; frame-ancestors 'none'` |
| `/static/dashboard.js` | GET/HEAD | **new** | embedded constant, `application/javascript`; no caching (`no-store` like everything) |
| `/api/state` | GET/HEAD | **new** | aggregate per data-model.md; ALWAYS 200 when the server is healthy, per-root errors inside the payload |
| any write method | POST/PUT/DELETE/PATCH | 405 | after the loopback peer gate (403 for non-loopback), unchanged |

## Security invariants (tested)

1. `make_server` refuses non-loopback hosts — no new flag anywhere (C-007).
2. `dashboard` alias cannot express a host at all.
3. Per-request loopback peer check precedes every response incl. 405s.
4. `/messages/<id>` response headers (incl. CSP) byte-identical to 0.16.0.
5. No route writes to any store: full-tree sha256 snapshot unchanged after
   ≥10 mixed requests across ≥2 roots (NFR-001, D9).
6. `/api/state` contains no `body` key at any depth (FR-003).

## JSON compatibility

- `/api/status`, `/api/messages` responses byte-shape-identical (NFR-005).
- `/api/state` is NEW; consumers key on `schema_version == 1`.

## Exit-code contract (unchanged globally)

0 ok · 1 wait-timeout · 2 usage/refusal/bind-failure · 3 superseded/stale ·
4 unknown rid · 5 partial fan-out · 130 SIGINT (where applicable).
