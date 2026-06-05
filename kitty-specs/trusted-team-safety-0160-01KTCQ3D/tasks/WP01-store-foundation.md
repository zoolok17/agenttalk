---
work_package_id: WP01
title: Identity registry, retirement & epoch store layer
dependencies: []
requirement_refs:
- FR-001
- FR-002
- FR-003
- FR-004
- FR-005
- FR-006
- FR-008
- FR-009
- FR-010
- FR-011
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
created_at: '2026-06-05T20:35:00Z'
subtasks:
- T001
- T002
- T003
- T004
- T005
- T006
- T007
history:
- date: '2026-06-05T20:35:00Z'
  event: created
  by: claude
authoritative_surface: src/agenttalk/
execution_mode: code_change
owned_files:
- src/agenttalk/store.py
- tests/test_store.py
tags: []
---

# WP01 — Identity registry, retirement & epoch store layer

## Objective

Build the entire **store-layer foundation** for the trusted-team safety release.
Everything else (CLI, threads) depends on this. You own `src/agenttalk/store.py`
and `tests/test_store.py` only — do not touch any other file.

Read first: `kitty-specs/trusted-team-safety-0160-01KTCQ3D/spec.md`,
`data-model.md`, `research.md` (especially **D1, D2, D3, D4**), and the RFC
`docs/rfc-identity-authz.md` §"Identity Registry", §"Global Epochs And Send-Time
Barriers", §"Retired Identities And Safe Rename".

Standing constraints: **stdlib only**; **history is immutable** (retire/rename/
forward mutate ONLY `config.json`, never message files); strictly **additive**
JSON (absent-not-null, except `epoch_at_send` which is a deliberate three-state —
see T006); Windows-first.

> Dev gotcha: tests run against the installed package. Run `pip install -e .`
> after editing `src/` or you will test the stale site-packages copy.

## Context: the existing surfaces you extend

- `config.json` today holds `agents` (active roster), optional `groups`,
  `roles`, `operator_facing`. `load_config()` (store.py ~476) validates
  fail-closed. Mutators: `add_agent`/`remove_agent`/`set_role`/`set_group`/
  `set_operator_facing` (~613–740).
- `Message.validate(roster)` (~323) checks `sender`/`recipient` are in `roster`.
- `Store.send()` (~797) guards `sender`/`recipient` against
  `cfg["agents"]` and rejects unknown kinds.
- `OPENER_KINDS = {"review-request","question","proposal"}` (store.py ~83).
- `valid_messages()` returns the validated message log (used by threads/check).
- The lexicographic message-`id` order is the bus's deterministic ordering.

## Subtasks

### T001 — `retired` registry shape + fail-closed validation
**Purpose**: Add the additive `retired` registry to `config.json`.

**Shape** (data-model §1):
```json
"retired": [
  {"name": "codex", "retired_at": "<iso>", "renamed_to": "codex-rev" | null, "reason": "<text>" | null}
]
```
**Steps**:
1. In `load_config()`, after the `roles` validation block, validate `retired`
   when present (absent ⇒ skip, full 0.15.0 behavior):
   - it is a list of dicts; each `name` passes `validate_agent_name`;
   - retired names are disjoint from active `agents` (an identity is active XOR
     retired — raise `ValueError` naming the offender if both);
   - retired names are unique within `retired` (no duplicate tombstones);
   - `renamed_to`, when non-null, passes `validate_agent_name`.
2. Extend the **case-insensitive uniqueness** check so it spans active ∪ retired
   (the *known roster*). A retired name must be unrepresentable as a new active
   name. (Reuse the casefold logic from `validate_agent_roster`; you may add a
   helper that validates the union.)
3. Wrap failures in the same `corrupt config at {path}: {e}` envelope used for
   `groups`/`roles`.

**Validation**: a config with `retired` overlapping `agents`, a duplicate
tombstone, or an unsafe name all raise a clear `ValueError` at load.

### T002 — active / retired / known roster helpers + history validation
**Purpose**: Keep retired identities valid for HISTORY while refused for SENDING
(research **D3** — the highest-risk change in this WP).

**Steps**:
1. Add `Store.active_agents()` → `config["agents"]` (list).
2. Add `Store.retired_agents()` → `[e["name"] for e in config.get("retired",[])]`.
3. Add `Store.known_agents()` → active ∪ retired (active first, then retired,
   de-duped).
4. Find every place that validates **historical** messages against the roster
   (e.g. where `valid_messages()` calls `Message.validate(roster)` / builds the
   roster passed to validation) and switch it to `known_agents()`. A message
   authored by a now-retired identity MUST still validate (FR-006).
5. Leave the `send()` guard and audience resolution on `active_agents()` (T004).

**Validation**: retire an agent, then a historical message from it still appears
in `valid_messages()` / passes `Message.validate(known_roster)`; a brand-new send
from it is refused (tested in T004).

> ⚠️ Be surgical: do not change what `resolve_audience`/`resolve_role_audience`
> use (they must stay active-only — you cannot broadcast to a tombstone).

### T003 — retire / rename / remove operations
**Purpose**: The roster admin ops (library layer; CLI wiring is WP03).

**Steps**:
1. `retire_agent(name, *, reason=None)`:
   - require `name` in active `agents` (else `ValueError`);
   - remove from `agents`; append `{name, retired_at:_now_iso(), renamed_to:None,
     reason}` to `retired`;
   - drop it from `roles`/`groups`/`operator_facing` (like `remove_agent` does)
     — but keep the tombstone. Single atomic `_write_config`.
2. `rename_agent(old, new, *, reason=None)`:
   - require `old` active; require `new` NOT in known roster (active or retired)
     — else `ValueError` (FR-002 non-rebindable, FR-005);
   - in ONE config write: retire `old` with `renamed_to=new`, add `new` as active,
     and **carry over** `old`'s role, group memberships, and `operator_facing`
     designation to `new` (so rename doesn't silently drop the liaison bit).
   - **B2 (Codex review)**: add the SAME non-rebindable guard to `add_agent` —
     refuse a name already in `known_agents()` (active OR retired) with a
     tombstone-aware error, at WRITE time. Do not rely on `load_config`
     fail-closing on the next read. (A `remove --force` name has no tombstone, so
     it stays re-addable — intended; cover both in T007.)
3. `_drain_check(name)` helper → returns a list of open thread descriptors that
   owe work to/from `name` (derive via `agenttalk.threads.derive_threads` over
   `valid_messages()`, filtering non-terminal threads involving `name`). Used by
   the CLI `rename --drain-check`. Keep it a pure query (no writes).
   - NOTE: `store.py` must not import `threads` at module top (threads imports
     store). Import `threads` lazily *inside* the method.
4. `remove_agent(name, *, force=False)`: change the existing `remove_agent` to
   require `force=True`. Without force, raise a dedicated exception/`ValueError`
   carrying the retire hint (the CLI turns this into exit 2 + hint text). With
   force, perform the existing removal (no tombstone — name stays re-addable) and
   return a marker the CLI uses to print the history-breakage warning.

**Validation**: rename carries role/liaison; `new` already-known is refused;
remove without force raises; with force removes and signals the warning.

### T004 — retired-send refusal
**Purpose**: A tombstone cannot send or be sent to (FR-004).

**Steps**:
1. In `send()`, keep the guard on `active_agents()`. Because retired names are
   removed from `agents`, a retired `sender`/`recipient` already fails the
   existing membership check — but make the error message explicit when the name
   is a retired tombstone (look it up in `retired_agents()` and say
   "‘codex’ is retired (a tombstone) and cannot send/receive; see `roster`").
2. Ensure the refusal is exit-2-mappable (raise `ValueError`, as today).

**Validation**: `send(sender=<retired>)` raises with a tombstone-specific message;
`send(recipient=<retired>)` likewise.

### T005 — single-hop retired forwarding (library) — B4 revised
**Purpose**: Forward a SPECIFIC owed request from a retired identity to a live
agent, auditable in transcript meta (FR-008). See data-model §3b, research D9.

**Steps**:
1. Add `forward_retired(retired_name, to_agent, request_id, *, from_agent=None,
   reason=None)`:
   - require `retired_name` ∈ `retired_agents()` (else `ValueError`);
   - require `to_agent` ∈ `active_agents()` (else `ValueError`);
   - require `request_id` resolves to a real thread **owed to/from**
     `retired_name` (derive via the lazily-imported `threads.derive_threads`
     over `valid_messages()`; refuse if no such owed thread) — this is what makes
     it *forwarding an obligation*, not a generic note;
   - **sender resolution**: `from_agent` if given (must be active); else
     `operator_facing()` if set; else raise asking for an explicit `--from`.
     NEVER default the sender to `to_agent`.
   - emit ONE ordinary message via `send()` (`kind="note"`) to `to_agent` with
     `meta.forwarded_from=retired_name`, `meta.forwarded_request_id=request_id`,
     `meta.forward={"hop":1}`, and audit-prose body;
   - refuse a second hop: if the source request's opener already carries
     `meta.forward` / `meta.forwarded_from`, raise (multi-hop forbidden).

**Validation**: forwarding a genuinely-owed request from a retired name to a live
agent emits the linked meta with the right sender; forwarding from an active
identity, to a retired target, for a non-owed request, without `--from`/liaison,
or a second hop, is each refused.

### T006 — epoch primitive: `current_epoch()` + `epoch_at_send` stamping
**Purpose**: The global epoch and automatic opener stamping (research **D1/D2**,
RFC §"Global Epochs"). Note: the `barrier bump` *command* is WP03; the store
layer just needs `current_epoch()` and the stamping in `send()`.

**Steps**:
1. `current_epoch()` → scan `valid_messages()` for messages whose
   `meta.get("barrier")` is a dict with `scope=="global"` and has `version` and
   `type`; return the `id` of the latest such message by message-id order, or
   `None` if none. Pure read. A malformed `meta.barrier` is simply ignored (does
   not count, does not crash).
2. In `send()`, when `kind in OPENER_KINDS` and the caller did NOT already supply
   `epoch_at_send` in `meta`, stamp `meta["epoch_at_send"] = current_epoch()`
   (which is a barrier id string, or `None` → serialized as JSON `null`). This is
   the deliberate three-state: an epoch-aware client always writes the key
   (id or null); pre-0.16.0 clients never had this code so the key is absent.
3. Do NOT stamp non-opener kinds. Do NOT stamp `barrier` messages themselves
   (they are `kind="message"`, not an opener).

**Validation**: with no barrier, an opener gets `epoch_at_send=None`; after a
barrier message exists, a new opener gets `epoch_at_send=<that barrier id>`; a
`note`/`message` gets no key.

### T007 — tests (`tests/test_store.py`)
Cover, at minimum:
- `retired` validation: overlap-with-active, duplicate tombstone, unsafe name,
  unsafe `renamed_to` all raise; a valid `retired` loads.
- `known_agents()` vs `active_agents()`; a historical message from a retired
  identity validates against the known roster.
- `retire_agent`: removes from active, adds tombstone, drops role/group/liaison.
- `rename_agent`: carries role+liaison; refuses `new` already-known (active OR
  retired — both cases); refuses `old` not active.
- **B2**: `add_agent`/`roster add` refuses a retired tombstone name at write time
  (config never written with the name in both `agents` and `retired`); a
  force-removed name (no tombstone) IS re-addable.
- `_drain_check` returns owed threads (construct a small fixture with an open
  review-request) and is empty when none.
- `remove_agent(force=False)` raises with the retire hint; `force=True` removes
  and signals the warning; retired name remains non-rebindable but force-removed
  name is re-addable.
- retired-send refusal (sender and recipient) with the tombstone-specific message.
- `forward_retired` (B4): forwarding a genuinely-owed `request_id` emits
  `meta.forwarded_from` + `meta.forwarded_request_id` with the resolved sender
  (`--from`/liaison, never the target); refuses non-owed request / active source /
  retired target / missing sender / second hop.
- `current_epoch()`: None with no barrier; returns latest barrier id by id-order
  with multiple barriers.
- `epoch_at_send` three-state in `send()` (absent for non-openers, null pre-
  barrier, id post-barrier).

## Branch Strategy

Planning branch: `master`. Final merge target: `master`. The execution worktree
for this WP's lane is allocated from `lanes.json` during `/spec-kitty.implement`;
do not create branches by hand.

Implement command: `spec-kitty agent action implement WP01 --agent claude`

## Definition of Done

- All T001–T007 complete; `pytest tests/test_store.py` green; full `pytest` green.
- `pip install -e .` then a manual smoke of retire/rename/forward/current_epoch.
- No file outside `owned_files` modified. No message file ever mutated.
- Codex cross-review (review-request over agenttalk, meta mission + wp_id WP01)
  returns approved.

## Reviewer guidance (for Codex)

- **D3 is the trap**: confirm history validation uses the KNOWN roster while
  send uses the ACTIVE roster — and that audience resolution did NOT accidentally
  switch to known (you must not broadcast to a tombstone).
- Confirm non-rebindable: `new` == a retired name is refused in `rename_agent`,
  and the case-insensitive uniqueness spans active ∪ retired.
- Confirm `epoch_at_send` is genuinely three-state and only on `OPENER_KINDS`.
- Confirm retire/rename/forward write ONLY `config.json` (grep for message writes).
