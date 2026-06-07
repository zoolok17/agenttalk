# Data Model — Review Hardening (0.18.0)

No persisted schema changes. This documents the additive in-memory/JSON shapes
and the new validation rule.

## 1. id validation rule (FR-003)

`store._ID_RE` (new), built from existing constants:

```python
_ID_RE = re.compile(r"\A\d{8}-\d{6}-\d{6}-[" + re.escape(_ID_ALPHABET) + r"]{4}\Z")
```

`Message.from_raw` rejects a non-matching `id` with `ValueError(f"malformed
id {id!r}")`. Effect: the file joins `list_invalid_messages` (parse/schema
class) and is quarantinable; it is never delivered and never advances a
cursor. No legitimately-generated id (always `_new_id`-shaped) is affected.

## 2. Signature guard (FR-001)

In `verify_message`, before `compare_digest`:
```python
if not isinstance(claimed, str):
    raise ValueError("signature is not a string")
```
No shape change — this only converts a would-be `TypeError` into the existing
`ValueError` "invalid signature" path.

## 3. Broadcast thread dict — additive `audience_retired` (FR-006)

`Thread.to_dict()` for a broadcast gains ONE additive key, emitted only when
non-empty (absent otherwise — additivity discipline):

```jsonc
{
  "request_id": "...",
  "is_broadcast": true,
  "audience": ["a", "b", "c"],        // FROZEN, unchanged (immutable history)
  "responded": ["a"],
  "pending": ["b"],                    // c removed: it is retired
  "audience_retired": ["c"],           // NEW, absent when no audience member is retired
  // next_owner / next_action (CLI layer) never name a retired member
  ...
}
```

`pending` and the `next_owner`/`await-reply` projection exclude current
retired identities. `audience` itself is never rewritten.

## 4. `.waiting` marker (FR-007) — already carries `pid`, no change

Existing shape (written at `cli.py:1654`), used as-is:
```jsonc
{"agent": "claude", "pid": 50824, "since": "...Z",
 "cursor_at_start": "...", "deadline_epoch": 1234567890.0}
```
FR-007 READS this; it writes nothing new. Readers already tolerate
absence/corruption (`read_waiting` → None).

## 5. Store helpers (FR-007 primitives, new in store.py)

```python
def _process_alive(pid: int) -> bool          # stdlib, fail-quiet (D3)
def foreign_wait_pid(self, agent: str, self_pid: int) -> int | None  # D4
```
`foreign_wait_pid` returns the live foreign owner's pid or None. Pure read;
no writes.

## 6. doctor report — additive marker liveness (FR-009)

`doctor`'s per-agent/diagnostic output gains an advisory note where a
`.waiting` marker exists: the marker `pid` and whether it is live, framed as
"a process is currently waiting as <agent> (PID x, alive)" — explicitly NOT a
claim of complete duplicate detection. JSON: additive under the existing
doctor structure (absent when no marker); plain output: one advisory line.
Never changes `doctor`'s exit code.

## 7. CLI warning text (FR-007)

One stderr line on `wait`/`listen` start when `foreign_wait_pid` is non-None,
e.g.:
```
warning: another live process (PID 50824) is already waiting as 'claude' in
this store. One window per agent is assumed; concurrent same-agent use can
lose cursor/threadstate updates.
```
Advisory only — no exit-code change, never blocks.

## 8. Validation rules summary

- id must match `_ID_RE` (else invalid/quarantinable).
- non-string signature → invalid (never `TypeError`).
- web message routes + `tail` validate against known roster (active∪retired).
- broadcast `pending`/`next_owner` exclude current retired; `audience_retired`
  lists them (absent when none); `audience` immutable.
- all new fields absent-not-null when unused; old on-disk formats tolerated.
