---
work_package_id: WP03
title: 'CLI wiring: tail parity, resume skip-retired, wait warning'
dependencies:
- WP01
- WP02
requirement_refs:
- FR-004
- FR-005
- FR-007
- FR-008
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
subtasks:
- T009
- T010
- T011
- T012
agent: "claude"
shell_pid: "84260"
history:
- '2026-06-07: created from approved plan rev2 (fffcb78, Codex pre-code approved)'
authoritative_surface: src/agenttalk/cli.py
execution_mode: code_change
owned_files:
- src/agenttalk/cli.py
- tests/test_cli.py
- tests/test_coordination.py
tags: []
---

# WP03 — CLI wiring: tail parity, resume skip-retired, wait warning

## Objective

Wire the user-facing behavior: `tail` shows retired history, `broadcast
--resume` survives a retired recipient, and `agenttalk wait` warns on a live
duplicate. `cli.py` + `tests/test_cli.py` + `tests/test_coordination.py` ONLY.

## Context

- spec FR-004 (tail), FR-005, FR-007, FR-008; research D4, D5, D7; contracts.
- `wait` is the ONLY blocking-wait command; `listen` is a skill that calls it
  (no `agenttalk listen` command — do NOT add one).
- WP01 provides `Store.foreign_wait_pid(agent, self_pid, now=, stale_after=)`
  and `_process_alive`. `STALE_THRESHOLD_SECONDS = 60.0` is at cli.py:317.
- resume loop is at cli.py:1218–1248 (`missed` → `store.send` per recipient,
  exit 5 on first failure). `_write_waiting_marker` at cli.py:1640.
- tail validates against active roster; switch to known roster like WP02 did
  for web. Find the tail command (`cmd_tail`) and its `m.validate(roster)`.

**Hard boundaries**: only the 3 owned files. Stdlib only. Exit-code contract
(C-004). Full suite green at HEAD.

## T009 — `tail` → known roster (FR-004 tail)

In the tail command, replace `roster = cfg.get("agents") or []` with the known
roster (active ∪ retired) — `store._known_roster(cfg)`, same as WP02/web.
Retired identities' history then prints instead of `TAIL INVALID`. No flag or
format change.

## T010 — `broadcast --resume` skip-retired (FR-005, D5)

In the resume block (cli.py:1213–1248), after computing `missed`:
1. Partition `missed` into active vs retired using the current roster:
   `retired = set(store.retired_agents())`;
   `to_send = [r for r in missed if r not in retired]`;
   `dropped = [r for r in missed if r in retired]`.
2. If `to_send` is empty (all remaining are retired): report `dropped`, print
   the manifest (json or plain), and **return 0** (resolved — not exit 5).
3. Else loop `store.send` over `to_send` only (was `missed`). On a send
   failure, keep the existing exit-5 partial path BUT include `dropped` in the
   manifest. On full success of `to_send`, print the resumed message + the
   `dropped` list and **return 0**.
4. Every manifest (json + plain, success + failure) gains `dropped=[…]` when
   non-empty (absent/empty otherwise — additive).
The net: a retired frozen recipient can never trap resume at exit 5; exit 5 is
emitted ONLY when an ACTIVE copy genuinely fails (narrows C-004, never
broadens).

## T011 — `wait` duplicate-activation warning (FR-007/008, D4)

In the `wait` command, BEFORE `_write_waiting_marker` overwrites the marker
(that call is the only point the prior owner is still visible), add:
```python
foreign = store.foreign_wait_pid(agent, os.getpid(),
                                 now=time.time(),
                                 stale_after=STALE_THRESHOLD_SECONDS)
if foreign is not None:
    sys.stderr.write(
        f"warning: another live process (PID {foreign}) is already waiting "
        f"as {agent!r} in this store. One window per agent is assumed; "
        f"concurrent same-agent use can lose cursor/threadstate updates.\n")
```
Advisory only: never `return`, never change the exit code, never block. A
stale or dead prior marker yields `foreign is None` → no warning (silent crash
recovery).

## T012 — Tests

`tests/test_cli.py`:
- `test_tail_shows_retired_history`: retire an agent with prior messages →
  `tail --from-start` prints them (not TAIL INVALID).
- `test_resume_skips_retired_exit0`: partial batch (audience_resolved=x,y, only
  x sent), retire y, run the resume command func → returns 0, manifest names
  `dropped=[y]`, no exit 5.
- `test_resume_active_failure_still_exit5`: a send failure on an ACTIVE missing
  recipient (monkeypatch `store.send` to raise for that name) → exit 5, manifest
  includes `dropped` if any.
- `test_wait_warns_on_live_duplicate`: monkeypatch `store.foreign_wait_pid` to
  return a fake pid → `wait` stderr contains the warning, exit code unchanged;
  monkeypatch it to return None → no warning.

`tests/test_coordination.py`:
- one e2e: two simulated `.waiting` markers / a live-vs-dead owner exercising
  the warning path end to end (use monkeypatched liveness to stay
  deterministic cross-platform). If WP02's `audience_retired` tripped an
  additivity gate that lives in this file, update it here (this file is
  WP03-owned).

## Definition of Done

- [ ] `pytest tests/test_cli.py tests/test_coordination.py -q` green; FULL suite green.
- [ ] Only the 3 owned files changed.
- [ ] No `agenttalk listen` command added; `wait` warning never changes exit code.
- [ ] Resume exit 5 only on a real active-copy failure.
- [ ] `pip install -e .` before testing.

## Reviewer guidance (Codex)

Focus: resume partition correctness + exit-code narrowing (5 only on active
failure, 0 when all-retired); the warning is strictly before marker overwrite
and never alters control flow/exit; tail known-roster switch; no new CLI
command; `store.foreign_wait_pid` called with cli's own staleness policy
(no store→cli import).

## Activity Log

- 2026-06-07T19:30:59Z – claude – shell_pid=84260 – Started implementation via action command
