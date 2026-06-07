---
work_package_id: WP01
title: 'Core: invalid-classification + liveness primitive'
dependencies: []
requirement_refs:
- FR-001
- FR-002
- FR-003
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
base_branch: kitty/mission-review-hardening-0180-01KTHP2E
base_commit: 5bf7800c414609c9e3765b057a07c40de0b02df7
created_at: '2026-06-07T18:57:49.715328+00:00'
subtasks:
- T001
- T002
- T003
- T004
- T005
shell_pid: '84692'
history:
- '2026-06-07: created from approved plan rev2 (fffcb78, Codex pre-code approved)'
authoritative_surface: src/agenttalk/store.py
execution_mode: code_change
owned_files:
- src/agenttalk/signing.py
- src/agenttalk/store.py
- tests/test_signing.py
- tests/test_store.py
tags: []
---

# WP01 — Core: invalid-classification + liveness primitive

## Objective

The foundation: make a poison signature and a malformed id behave like normal
invalid (quarantinable) messages, and add the stdlib liveness primitive +
foreign-wait detector that WP03/WP04 consume. `signing.py` + `store.py` +
their tests ONLY.

## Context

- spec FR-001, FR-002, FR-003, FR-007 (primitives); research D1, D2, D3, D4;
  data-model §§1,2,5.
- `signing.verify_message` is at `src/agenttalk/signing.py:303`; the
  vulnerable line is `claimed = meta["signature"]` (326) → `compare_digest`
  (328). All callers catch only `ValueError`.
- `Message.from_raw` is at `src/agenttalk/store.py:330`; its id/ts string
  check is the block at lines 343–351. `_new_id` (1887) format:
  `"%Y%m%d-%H%M%S-%f"` + `"-"` + 4×`_ID_ALPHABET`; `_ID_ALPHABET =
  string.ascii_letters + string.digits` (store.py:34).
- `.waiting` marker shape (written `cli.py:1654`): `{"agent","pid","since",
  "cursor_at_start","deadline_epoch"}`; `read_waiting` (store.py:1675) →
  dict|None, never raises.

**Hard boundaries**: only the 4 owned files. Stdlib only. `store.py` must NOT
import anything from `cli.py` (Codex note — freshness policy comes in as
params). Full suite must stay green at your HEAD.

## T001 — Signature type-guard (FR-001)

In `verify_message`, immediately before `claimed = meta["signature"]` /
`compare_digest`, add:
```python
claimed = meta["signature"]
if not isinstance(claimed, str):
    raise ValueError("signature is not a string")
```
This converts a would-be `TypeError` (when `claimed` is a list/number) into
the existing `ValueError` "invalid signature" path, so every read-path gate
(`messages_for`, `valid_messages`, `list_invalid_messages`, `current_epoch`,
quarantine selection) degrades gracefully and the file becomes quarantinable.
No other change to verify_message.

## T002 — id-shape validation (FR-003, C-008)

In `store.py`, add a module-level constant near `_ID_ALPHABET`:
```python
_ID_RE = re.compile(r"\A\d{8}-\d{6}-\d{6}-[" + re.escape(_ID_ALPHABET) + r"]{4}\Z")
```
(`re` is already imported; confirm.) In `Message.from_raw`, AFTER the existing
id/ts non-empty-string check (store.py:346–351), add:
```python
if not _ID_RE.match(data["id"]):
    raise ValueError(f"malformed id {data['id']!r} (not a generated message id)")
```
Effect: a non-generated id (e.g. `"zzzz"`) is classified invalid at scan time
→ quarantinable, never delivered, never advances a cursor.
**Scope honesty (C-008)**: this rejects wrong-SHAPE ids only. Do NOT add any
clock-skew handling; well-formed future-dated ids are out of scope (documented
in WP04).
**Back-compat**: every real id matches `_ID_RE`. The microsecond field is
always 6 digits (`%f`), incl. the `_new_id` `+1µs` monotonic bump.

## T003 — `_process_alive(pid)` (FR-007 primitive, D3, C-001/005)

Add a stdlib, fail-quiet module function in `store.py`:
- guard: `if not isinstance(pid, int) or pid <= 0: return False`.
- POSIX (`os.name != "nt"`): `os.kill(pid, 0)` → return True;
  `ProcessLookupError` → False; `PermissionError` → True (exists, not ours);
  other `OSError` → False.
- Windows (`os.name == "nt"`): via `ctypes`,
  `OpenProcess(0x1000, False, pid)` (`PROCESS_QUERY_LIMITED_INFORMATION`);
  if handle is falsy → False; else `GetExitCodeProcess` and treat
  `STILL_ACTIVE` (259) as alive, `CloseHandle`, return the verdict. Wrap the
  whole Windows branch in `try/except Exception: return False`.
- The function NEVER raises.

## T004 — `Store.foreign_wait_pid` (FR-007 detector, D4)

```python
def foreign_wait_pid(self, agent: str, self_pid: int, *,
                     now: float | None = None,
                     stale_after: float | None = None) -> int | None:
```
- read `self.read_waiting(agent)`; None → return None.
- extract `pid` (int) and freshness: a marker is fresh if its
  `deadline_epoch` (when a number) has not passed by more than `stale_after`
  seconds relative to `now`, OR — mirroring `status`'s logic — defer to the
  caller's policy. Keep it self-contained: default `now = time.time()` and
  `stale_after = 300.0` (a store-local default constant), but accept overrides
  so `cli.py` can pass its `STALE_THRESHOLD_SECONDS`. Do NOT import cli.
- return `pid` only when: `isinstance(pid, int)`, `pid != self_pid`, the
  marker is fresh, AND `_process_alive(pid)`; else None.
- never raises (wrap reads defensively).

## T005 — Store/signing tests

In `tests/test_signing.py` and `tests/test_store.py` (follow existing style):
1. `test_non_string_signature_is_invalid_not_crash`: signing enforced; a
   message dict with valid version/alg but `signature=[1,2,3]` → `verify_message`
   raises `ValueError` (not TypeError). Store-level: write that poison file +
   a legit message → `messages_for`, `list_invalid_messages`, `current_epoch`
   all return normally; the poison id is in `list_invalid_messages`.
2. `test_malformed_id_rejected`: hand-write a roster-valid file with
   `id="zzzz"` → it appears in `list_invalid_messages`, NOT in
   `messages_for(recipient)`; a normal `send` afterwards is still delivered
   (cursor not poisoned). Also unit-test `_ID_RE` against 1000 freshly
   `_new_id()`-generated ids (all match) and a handful of malformed ones.
3. `test_process_alive`: `_process_alive(os.getpid())` is True;
   `_process_alive(2**31-1)` is False (almost-certainly-dead pid);
   `_process_alive(0)`/`(-1)`/`("x")` are False; never raises.
4. `test_foreign_wait_pid`: write a `.waiting` marker with `pid=os.getpid()`
   (self) → detector returns None (same pid); with a dead pid → None
   (not alive); with a live foreign pid simulated via monkeypatching
   `_process_alive`→True and pid≠self, fresh marker → returns that pid;
   stale marker (old deadline) → None.

## Definition of Done

- [ ] `pytest tests/test_signing.py tests/test_store.py -q` green; FULL suite green.
- [ ] Only the 4 owned files changed; `store.py` imports nothing from `cli.py`.
- [ ] `_ID_RE` derived from `_ID_ALPHABET` (not hand-copied).
- [ ] `_process_alive` + `foreign_wait_pid` never raise.
- [ ] `pip install -e .` before testing.

## Reviewer guidance (Codex)

Focus: the guard truly precedes compare_digest; `_ID_RE` accepts every real id
(test the monotonic bump near rollovers) and rejects `zzzz`; Windows liveness
ctypes correctness + total fail-quiet; foreign_wait_pid self-vs-foreign and
fresh-vs-stale logic; no cli import in store.
