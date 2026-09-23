# Increment C — stray-draft audit at turn end

Branch `fix/wrapper-reply-channels`, on top of increments A (`6697cc7`) and B (`7ffe359`). Closes
the observability gap the other two increments' own root cause depended on: "nothing else ever
revisits a head once it commits clean" (an existing loop.py comment, quoted verbatim) is exactly
why the three lost replies from the field facts sat unpublished until the lead found and delivered
them BY HAND from another process — no part of the wrapper itself ever looked at the drafts
directory again after the orphaning turn committed.

## The fix

1. **Detection** (`reply_transport.py`, new `stray_reply_drafts(store, agent, *, exclude_id=None)`).
   Each turn writes its own deterministic `<id>.md` (`loop._with_reply_draft`) and a successful
   delivery unlinks it — so any OTHER *live* `.md` file surviving in the agent's own drafts
   directory was orphaned by a past turn. "Live" is `len(path.suffixes) == 1` (only the trailing
   `.md`) — every sidecar this module already tracks (`.refused.md`, `.refused.reason.txt`,
   `.superseded.md` from increment A, `.interrupted.md`) carries a second suffix and is therefore
   already an accounted-for outcome, not silent loss. `exclude_id` lets the caller name the CURRENT
   turn's own id so it is never flagged as stray relative to itself.
2. **Active notice, once per file** (`loop.py`, new `_notify_stray_reply_draft` +
   `_report_stray_reply_drafts`). Mirrors `_notify_reply_refusal`'s own target resolution
   (`operator_facing() or sole_lead()`) and never-raise contract exactly. A new
   `reply_transport.stray_draft_notified_path` sidecar (`<id>.stray-notified.txt`) dedupes the
   ACTIVE bus notice to once per file — an ongoing stray still counts toward the health-warnings
   list on every subsequent scan (see below), but re-notifying the lead every single tick for the
   same never-cleaned file would be noise, not signal. The orphaned draft file itself is left
   completely untouched (never deleted, never renamed) — an operator inspecting the notice can read
   the child's own unpublished answer directly.
3. **Health warnings** (`health.py`, `WrapperHealthWriter.idle` gains a `warnings: list[str] | None`
   parameter, threaded straight to `_write`/`build_snapshot`). Unlike
   `_has_unresolved_reply_refusal` (which re-derives its own answer from disk on every `idle()`
   call), `warnings` here is the CALLER's already-computed list — the caller (loop.py) already paid
   for the directory scan this same tick when it ran `_report_stray_reply_drafts`, and a second,
   independent scan inside `health.py` would just double the I/O for no new information. Each
   warning label (`stray_reply_draft:<id>`) is plain alphanumeric+hyphen+colon, confirmed to pass
   `health.py`'s own `_SAFE_TOKEN_RE` sanitizer unmodified.
4. **Wiring** (`loop.py`). Both `_deliver_reply_draft` call sites (the continuous loop's
   commit-gate success path and the one-shot equivalent) now also call
   `_report_stray_reply_drafts(store, agent, record)` right after delivering the CURRENT turn's own
   draft, and thread the result into `on_health_idle(..., warnings=stray_draft_warnings)`.

## An honest limitation found while wiring this in, not smoothed over

`run_loop` has TWO distinct successful-turn code paths: the `commit_gate is not None` branch (where
I wired increment C) and an older "legacy no-admission" branch reached when `commit_gate is None`.
**The legacy branch never calls `on_health_idle` at all on a successful turn** — this predates
increment C entirely; I did not introduce it, and I did not attempt to fix it (undertaking to
understand and safely extend a ~100-line branch I have not otherwise touched, with an unclear
reason for the asymmetry, is outside this increment's own scope and carries real regression risk
for a mechanism I don't yet fully understand).

This matters for coverage, stated precisely: `cli.py`'s own real `agenttalk wrap --loop` invocation
**always** passes `commit_gate=commit_gate` (confirmed by reading the call site) — so production
traffic exclusively uses the branch this increment covers, and the stray-draft audit is live for
every real wrapped seat. It is this repo's OWN unit test suite that exclusively exercises the
legacy branch — no test anywhere in `tests/` constructs a `commit_gate` for `run_loop` (confirmed
by a repo-wide grep) — which is why my own first attempt at an end-to-end `run_loop`-based test
silently observed zero stray warnings and had to be replaced with a more surgical test that drives
`_deliver_reply_draft` + `_report_stray_reply_drafts` directly, the same two calls the real
commit-gate branch makes in the same order. Flagged for the lead's own call on whether the legacy
branch is dead code worth removing, or a real second path that deserves its own follow-up.

## Tests

- `reply_transport.py`: `test_stray_reply_drafts_finds_orphans_excludes_sidecars_and_current` -
  every sidecar suffix correctly excluded; the current id excluded only when named;
  `exclude_id=None` counts everything live.
- `loop.py`: `test_report_stray_reply_drafts_notifies_once_but_keeps_reporting` - first scan
  notifies + returns the warning; second scan (nothing changed) still returns the SAME warning but
  sends no second notice.
- `loop.py`: `test_current_turn_delivery_unaffected_by_an_unrelated_past_orphan` - drives the exact
  two-call sequence the real commit-gate branch uses; confirms the current turn's own delivery is
  untouched by an unrelated orphan, the orphan is reported, and the orphan's own bytes are left
  exactly as they were.
- Full targeted run: `test_reply_draft_delivery.py` 42/42 (39 existing + 3 new), `test_wrapper_
  health.py` 19/19, `test_wrapper_loop.py -k "reply_draft or health or refusal"` 5/5,
  `test_dead_letter.py` (a heavy `run_loop`/`make_drive` consumer, spot-checked for regressions
  from the two edited call sites) 72/72, `test_stub_agent_canary.py` 7/7. `ruff check` +
  `py_compile` clean on every touched file.

## Confidentiality sweep

`grep -riE '<protected-1>|<protected-2>'` (the two protected strings of the local confidentiality
rule) over this file and the increment's own diff — 0 hits. Public GitHub repo; no client name,
credential, or hostname belongs in any tracked content regardless.
