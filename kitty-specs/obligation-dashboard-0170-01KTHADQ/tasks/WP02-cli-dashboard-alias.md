---
work_package_id: WP02
title: 'CLI wiring: dashboard alias + bind-failure handling'
dependencies:
- WP01
requirement_refs:
- FR-004
- FR-006
- FR-010
planning_base_branch: master
merge_target_branch: master
branch_strategy: Single serial lane branched from master; squash-merge back to master at mission end.
subtasks:
- T008
- T009
- T010
history:
- '2026-06-07: created from approved plan rev2 (8e81ace, Codex pre-code review approved)'
authoritative_surface: src/agenttalk/cli.py
execution_mode: code_change
owned_files:
- src/agenttalk/cli.py
- tests/test_cli.py
tags: []
---

# WP02 — CLI wiring: `dashboard` alias + bind-failure handling

## Objective

Wire WP01's server surface into the CLI: the new `agenttalk dashboard`
subcommand with repeatable `--store`, shared dispatch through `cmd_serve`,
FR-010 bind-failure handling (exit 2, actionable), and per-spelling startup
messages. `src/agenttalk/cli.py` + `tests/test_cli.py` ONLY.

## Context

- `contracts/cli-surface.md` — the exact CLI contract (serve byte-identical
  except FR-010; dashboard surface; exit codes).
- `research.md` D3 (CLI shape), D4 (startup warn, never refuse), D10
  (bind-failure handling + the live WinError 10013 repro).
- WP01 (merged before you start) exports `RootDescriptor` and `make_server`
  with the additive `extra:` kwarg; `/dashboard` is the alias's landing
  route.
- spec FR-004, FR-006, FR-010; NFR-002(a).

**Hard boundaries**: don't touch `web.py` (WP01-owned), `store.py`,
`threads.py`, docs (WP03). Stdlib only. `serve`'s parser flags stay
byte-identical.

## Subtask T008 — `dashboard` subparser + `--store` plumbing + shared dispatch

**Steps**:
1. Refactor `cmd_serve(args)` minimally so both spellings share it. Derive
   behavior from `getattr(args, "stores", None)` and a `landing` default
   set per subparser (`set_defaults(landing="/")` for serve,
   `landing="/dashboard"` for dashboard).
2. Root resolution:
   - No `--store` (both spellings): today's `_get_store(args)` —
     single root, unchanged semantics including exit-2 on no store.
   - With `--store` entries (dashboard only): for each path build
     `Store(Path(p))` the same way `--root` resolution does for an explicit
     root (NO upward walk from each store path — the path IS the project
     root; mirror how `_get_store` constructs a Store for an explicit
     `--root`, must_exist semantics included BUT do not exit: a
     missing/uninitialized store path WARNS to stderr and is still passed
     through — per-root errors are data (D4/FR-005). Note: construct
     descriptors with the path even if `.agenttalk/` is absent; web's
     `build_state` reports it via `errors[]`.)
   - First store = root[0]; labels via `web._dedup_labels`-equivalent
     (WP01 exports it or RootDescriptor construction takes raw paths —
     use whatever WP01 landed; do not duplicate label logic if exported).
3. Build the server: `_web.make_server(first_store, "127.0.0.1", args.port,
   quiet=args.quiet, extra=[RootDescriptor(...) for the rest])` for
   dashboard; the serve path keeps its EXACT current call (host from
   `args.host`).
4. New subparser:
   ```python
   pdb = sub.add_parser(
       "dashboard",
       help="Multi-root obligation dashboard (read-only, loopback-only). "
            "Same server as `serve`, opening on the hierarchy view. "
            "Repeat --store to watch several projects in one tab.",
   )
   pdb.add_argument("--port", type=int, default=8765, ...)
   pdb.add_argument("--store", action="append", dest="stores", metavar="PATH",
                    help="Project root to watch (repeatable; default: the "
                         "resolved current project)")
   pdb.add_argument("--quiet", action="store_true", default=True, ...)
   pdb.add_argument("--access-log", dest="quiet", action="store_false", ...)
   pdb.set_defaults(func=cmd_serve, landing="/dashboard")
   ```
   NO `--host` argument (NFR-002a: tested as unknown option). serve's
   subparser gains only `set_defaults(landing="/")` (and `stores=None`) —
   no new flags.

**Validation**: `agenttalk dashboard --help` shows port/store/access-log
only; `agenttalk serve --help` unchanged.

## Subtask T009 — Bind-failure handling + startup messages

**Steps**:
1. Wrap the `make_server` call (shared path, so both spellings get it):
   ```python
   except ValueError as e:        # existing non-loopback refusal — keep first
       sys.stderr.write(f"agenttalk serve: {e}\n"); return 2
   except OSError as e:           # NEW (FR-010, D10)
       sys.stderr.write(
           f"agenttalk {spelling}: could not bind 127.0.0.1:{args.port} — {e}\n"
           f"  Another program is probably listening on this port.\n"
           f"  Try `--port 0` (OS picks a free port) or another --port.\n"
       ); return 2
   ```
   Use the actual host variable for serve (it has `--host`). `spelling`
   from `args.landing` or a dedicated default — message must name the
   command the user typed.
2. Startup messages: serve prints the `/` URL exactly as today; dashboard
   prints `_format_url(...)` + `dashboard` path, e.g.
   `serving obligation dashboard at http://127.0.0.1:8765/dashboard`.
3. Startup WARN (D4): for each `--store` path lacking `.agenttalk/`, write
   `warning: <path> has no .agenttalk store yet — it will appear as a
   degraded root until initialized` to stderr. Non-fatal.

**Validation**: quickstart §6 (bind a busy port → exit 2 + message;
`--port 0` works).

## Subtask T010 — CLI tests

In `tests/test_cli.py` (follow the file's `_run`/capsys conventions):

1. `test_dashboard_help_surface`: `dashboard --help` exits 0; help text
   contains `--store` and `--port`; does NOT contain `--host`.
2. `test_dashboard_rejects_host_option`: `dashboard --host 0.0.0.0 --port 0`
   → SystemExit code 2 (argparse unknown option). This is NFR-002(a) — the
   alias has no host surface at all.
3. `test_serve_bind_failure_exit2`: bind an ephemeral socket first
   (`socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]`),
   keep it open, run `serve --port <port>` via the command func → returns 2;
   stderr names `127.0.0.1:<port>` and suggests `--port 0`. Same assertion
   for `dashboard --port <port>` (message names `dashboard`).
4. `test_dashboard_store_plumbing`: two initialized tmp stores; run the
   dashboard command func with `--store a --store b --port 0` but
   monkeypatch `web.make_server` to capture `(store, host, port, extra)`
   and return a dummy with `server_address`/`serve_forever` raising
   KeyboardInterrupt → assert first store root == a, one extra descriptor
   for b, host == "127.0.0.1". (Avoids real serving in CLI tests; web
   behavior is WP01-tested.)
5. `test_dashboard_missing_store_warns_not_fatal`: `--store <empty dir>`
   + monkeypatched server as above → exit 0, stderr contains the warning,
   descriptor still passed through.
6. `test_serve_parser_unchanged`: `serve --help` still lists `--host`
   (loopback values), `--port`, `--access-log`; no `--store`.

## Definition of Done

- [ ] `python -m pytest tests/test_cli.py -q` green; FULL suite green.
- [ ] No changes outside `src/agenttalk/cli.py` + `tests/test_cli.py`.
- [ ] `serve` parser flags byte-identical (only `set_defaults` additions).
- [ ] Exit-code contract intact: bind failure 2, usage 2, clean Ctrl+C 0.
- [ ] `pip install -e .` run before testing (dev gotcha).

## Reviewer guidance (Codex)

Focus: the shared `cmd_serve` refactor not perturbing serve's behavior
(startup message string, exception order ValueError-before-OSError, exit
codes), `--store` resolution NOT doing an upward walk per path, the warn-
not-refuse contract (D4), and the NFR-002(a) test shape (unknown option,
not non-loopback refusal).
