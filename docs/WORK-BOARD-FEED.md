# Work-board feed reference

Audience: console integrators consuming the read-only bus-derived board.

`GET /api/work-board?root=<project-id>` selects a configured root using the same
rules as the other console feeds. Unknown roots return HTTP 400. The response is
JSON, with no cursor or history endpoint. It never publishes work or grants merge
authority. The shared snapshot worker computes the projection; polling does not
read message files, run Git, or query external CI.

Schema version 1 includes `target_root_project_id`, `generated_at`, `coverage`,
`items`, `legacy`, `unassigned`, `total_count`, `truncated`, `omitted_count`,
`errors`, and `window_days`. During initial discovery, coverage is `building` and
the total is unknown. After a refresh failure, available cards are `last_known`;
consumers must show degraded coverage and must not present these as current Ready.

The feed includes active items and Done items whose approximate last work event
falls within seven UTC days. Entirely cancelled/declined work without remaining
obligations is excluded only when its history is complete. Unknown items remain
visible. Display order uses event time and item key, not causal workflow order.
Integration is unknown until a separate observer supplies evidence; task completion
alone never means Done. External CI is labelled `local checks not tracked`.

There are at most 100 tagged cards and 256 KiB of UTF-8 JSON, including groups and
coverage. Overflow omits whole cards, sets `truncated`, and reports known totals
and omissions; incomplete totals are null. One counted Legacy group and the
unassigned group sit outside the card count. Oversized group details can be omitted
with `groups_truncated`. A card too large to fit is counted as omitted.

Coverage budgets the selected history across active and compacted partitions,
including linked untagged threads, before applying the card limit: 50,000 envelopes
and 128 MiB source bytes. At 60% it warns to schedule archive indexing. Overflow
retains last-known placement with `capacity_exceeded`; it cannot certify a partial
reduction. Coverage also reports scan times, valid-until, generation and discovery
statistics. Freshness expires 15 seconds after scan start.

Local checks use `wb.<item>.c<cycle>.<check>` with check keys matching
`[a-z0-9-]{1,24}`, scope `<project-id>/<item>/c<cycle>`, and the exact full candidate
revision. Only validated blocker-green evidence satisfies a required check. Missing
revisions, wrong scopes, advisory greens and another item's gates cannot satisfy it.
Global barriers remain separately visible on cards and keep their ordinary effect
on `check --gates`.

Root checks, non-board scoped checks, and both attention feeds exclude `wb.` gates.
Adding these names to root `required_gates` is refused. Legacy root-list entries
are ignored with a configuration warning. Malformed gate state still fails closed.
Board-gate cleanup is separate future maintenance, never a GET side effect.
