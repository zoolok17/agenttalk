# Scope: more than one OVH model behind the qwen gateway

Status: scoping note, no code. Written against `master` at `9ce4cba0` (v0.91.1 plus the #193 docs). Line
numbers are at that commit. No spend, no gateway command other than what is read from the code; nothing
in this note was checked against OVH's own catalogue or API (section 3 says how to).

Why: the operator uses `ovh-qwen` seats as overflow capacity when the subscription allowances are tight.
Measured on Qwen3.8-27B through the gateway: roughly EUR 2-3 per busy gateway-hour, with about 98% of
tokens being input (about 77K input tokens re-sent per call, no prompt caching on OVH). A cheaper model
could cut cost several times over if quality holds; we want to trial one on one bounded card, then decide.

## Summary and recommendation

1. Today nothing about the model is configurable: the alias, the four rates, the context and output
   limits are module constants (`ovh_gateway.py:28-44`) read at about a dozen sites, and the wrapper and
   supervisor each carry their own literal `"Qwen3.8-27B"` (`wrapper/run.py:490-492`, `supervisor.py:8823`).
   A trial on a cheaper model therefore needs code, not just a re-init.
2. Recommended: **option A2**, "one model per install, chosen at `gateway init`". It is the same move
   `12cc78a` made for the spend envelope: a vetted table of model profiles, `gateway init --model <alias>`,
   the choice pinned into the ledger and its hash, and every reader (front, reserve, settle, canary,
   LiteLLM config, wrapper, supervisor) reading the stored profile instead of a constant. Effort **M**.
3. Not recommended for the trial: **B**, several models under one ledger with per-child-turn selection,
   per-model canaries and a child-cap schema bump. Effort **L**, and its extra parts (schema migration,
   per-model canary state) are not needed to answer "does the cheaper model hold up".
4. A1 (swap the constants on a throwaway branch and install that build) is the cheapest, **S**, but it
   touches the same sites A2 has to touch anyway, leaves an off-release gateway on the operator's host, and
   is throwaway work. Only choose it if the answer is needed within a day or two.
5. Where to trial: on the second gateway host, not the primary one. The ledger, tokens and ports are
   per host (section 0), so one host runs one model at a time, and every model switch on a host is a
   re-init that starts a new ledger. I am inferring from #194 that a second gateway exists (its wording
   is "two gateways"); please confirm which host that is.
6. Suggested first candidate: `qwen3-coder-30b-a3b-instruct`. It is non-reasoning (avoids the uncapped
   thinking-token behaviour in #194 item 4 and the reasoning-merge workaround), the same family as today's
   model, and about 7x cheaper on the input-dominated mix at the quoted list prices. `gpt-oss-120b` is a
   reasoning model with a probably smaller context window and is a second choice.

## 0. What is pinned today

Everything below is the single-model assumption. Rates are micro-EUR per 1M tokens.

| Fact | Where |
|---|---|
| Model alias `Qwen3.8-27B` | `ovh_gateway.py:28` |
| Settlement rates 400000 in / 2700000 out; reservation rates +20% (480000 / 3240000); observed date and source page | `ovh_gateway.py:29-36` |
| Context 262144 tokens; max output 32768 tokens | `ovh_gateway.py:37`, `:44` |
| Envelope, child caps, canary tolerance (10%), ports 4000/4001, request cap 512 KiB | `ovh_gateway.py:45-63` |
| `price_policy()` puts alias, rates, limits and the reservation figure into one dict; `price_policy_hash()` hashes it | `ovh_gateway.py:135-196` |
| `child_cap_policy()` hashes max calls, per-turn EUR, per-turn seconds and the one-attempt reservation | `ovh_gateway.py:199-225` |
| Settlement and reservation cost come from the global rates | `ovh_gateway.py:119-132` |
| Ledger metadata stores `price_policy_hash`, the envelope, `child_cap_policy_hash`; the marker repeats the hash | `ovh_gateway.py:568-615` |
| Every ledger read recomputes the hash from the stored envelope plus the live pricing constants | `ovh_gateway.py:836-849` |
| `reserve` / `reserve_for_child` refuse any model other than the alias; `settle` marks the attempt `uncertain` and holds when the response model differs | `ovh_gateway.py:1642`, `:1678`, `:1826` |
| `attempts` rows already record `model` and the policy hash per attempt | `ovh_gateway.py:658-666`, `:1601-1609` |
| Settlement is `uncertain` (a hold) if input or output exceeds the pinned limits or actual exceeds the reservation | `ovh_gateway.py:1861-1866` |
| Canary attempt must be settled, model equals the alias, policy hash matches; comparison is one attempt vs one dashboard delta | `ovh_gateway.py:929-940`, `:2033-2117` |
| LiteLLM config has exactly one `model_list` entry, `openai/<alias>`, against the unified OVH base | `ovh_gateway.py:2301-2341`; base pinned at `ovh_gateway_service.py:72` |
| Front rejects a request whose `model` is not the alias (422) or whose `max_tokens` exceeds the pinned output (422) | `ovh_gateway_front.py:309`, `:315-321` |
| Front serves one request at a time | `ovh_gateway_front.py:205`, `:324` |
| Seat: `cfg_agent["model"]` must equal the alias; `ANTHROPIC_MODEL` is set to it | `cli.py:12017-12019`, `:12088-12092` |
| Seat: literal alias and literal `32768` output cap, checked again when the child env is built | `wrapper/run.py:68`, `:490-499`; `MAX_THINKING_TOKENS=0` at `:501` |
| Supervisor bootstrap check: model must be the literal alias | `supervisor.py:8823-8824` |

Two structural facts shape every option:

- **Per host, not per project.** Ports 4000/4001 are constants, and the ledger, secret directory and tokens
  resolve from per-user default paths (`ovh_gateway.py:58-63`, `:235-262`). One host can run one gateway, and
  so one model pin. Two models at once means two hosts, or making ports and directories configurable
  (extra work, not in any option below).
- **The model is already chosen by name in the request body** on OVH's unified endpoint, so a second model
  needs no new base URL (`ovh_gateway_service.py:60-72`).

Cost arithmetic for orientation. It uses the third-party list figures quoted in the request as if they were
EUR, because I have not seen OVH's EUR tariff for the candidates. For one typical call of 77K input and 1K
output tokens, and for the one-attempt reservation at today's pinned 262144 / 32768 limits:

| Model | in / out per 1M | one call (77K in, 1K out) | vs Qwen3.8-27B | reservation (+20%) |
|---|---|---|---|---|
| Qwen3.8-27B (third-party figure) | 0.47 / 3.00 | 0.0392 | 1.0x | (ledger, EUR tariff: 0.231999) |
| qwen3-coder-30b-a3b-instruct | 0.07 / 0.26 | 0.0057 | about 6.9x cheaper | 0.0322 |
| gpt-oss-120b | 0.09 / 0.47 | 0.0074 | about 5.3x cheaper | 0.0468 |

The reservation shrinking is a side benefit: `_assert_external_envelope` requires opening balance plus
cutoff plus one reservation to fit under the ceiling (`ovh_gateway.py:462-475`), so a smaller reservation
leaves more headroom for the cutoff.

## 1. Options

### A1. Trial build with swapped constants (cheapest, throwaway)

Change on a branch, install that build on the trial host, re-init. What exactly changes:

- **Model alias**: `ovh_gateway.py:28`; the seat and supervisor literals `wrapper/run.py:68`, `:490-492`,
  `supervisor.py:8823-8824`. Three places carry a literal that does not import the constant, so they can
  drift; a swap that misses one fails at launch, not at test time.
- **Rates, limits, source, date**: `ovh_gateway.py:29-44`. All from OVH's own catalogue (section 3).
- **LiteLLM config**: derived from the alias by `render_litellm_config` (`:2322-2324`); nothing to edit, but
  the config hash bound into the manifest changes, so this is a fresh `init`, not `reconfigure`.
- **Price policy and hash**: change automatically with the constants (`:135-196`); the ledger, install
  marker, manifest, task identity and runtime marker all follow the ledger's hash (since #191), so nothing
  else edits by hand. Every pinned test hash and doc hash changes.
- **Reservation size and child caps**: `reservation_cost_micro_eur()` (`:127-132`) follows the new rates and
  limits; `child_cap_policy_hash` includes it (`:212`), so caps must be reinstalled (`cap-install`).
- **Seat launch `--model`**: the seat's `model` in supervisor config must equal the new alias
  (`cli.py:12017`); the runtime fingerprint changes and the wrapped session resets (section 2).
- **Canary**: a new policy hash invalidates every old canary by design (`ovh_gateway.py:929-940`); a fresh
  one is mandatory before seats start.

Risks: a non-release build on the operator's host; drift between three hand-copied literals; and the
tests that pin the alias or hash (`tests/test_ovh_gateway_cli.py`, `test_ovh_wrapper_profile.py`,
`test_supervisor.py` carry literals; most other tests use the symbol). Effort **S**.

### A2. Model profile chosen at init, one model per install (recommended)

The pattern of `12cc78a`, applied to the model:

- A vetted profile table in one module (alias, settlement rates, reservation margin, max context, max
  output, source URL and observed date). Vetted in code and reviewed by PR, not operator-typed, for the same
  reason the API base is a code constant (`ovh_gateway_service.py:60-72`): a runtime-supplied price or
  endpoint is an exfiltration and under-billing lever. `gateway init --model <alias>` selects one entry;
  the default stays `Qwen3.8-27B`, so an unchanged invocation produces the byte-identical hash.
- The ledger pins the chosen alias (and the profile it was priced under) in its metadata and folds them into
  `price_policy_hash`, exactly as the envelope is folded in (`ovh_gateway.py:568-615`, `:836-849`).
- Every reader takes the profile from the ledger, never the constant. The list is the table in section 0:
  `price_policy`, `settlement_cost`, `reservation_cost`, `reserve` / `reserve_for_child` / `settle`
  (`:1642`, `:1678`, `:1826`, `:1861-1866`), the canary checks (`:937`, `:2062`), `render_litellm_config`
  (`:2322-2324`), the front (`ovh_gateway_front.py:309`, `:315-321`, plus `FrontConfig` limits at `:74-95`),
  `SpendLedger.status()` (add the alias and profile), the seat check (`cli.py:12017`, `:12091`), the
  child-env builder (`wrapper/run.py:490-499`) and the supervisor check (`supervisor.py:8823`). The three
  literal copies become one lookup, which removes the drift risk in A1.
- The manifest already stores `price_policy_hash` and the config hash; the alias appears in the LiteLLM config,
  whose sha256 is in the manifest, so a manifest written for one model fails against a ledger for another
  (the same property #191 gave the envelope).
- Migration of an existing 0.91.1 ledger: none needed for the default model, because the default profile
  reproduces today's hash. Switching an existing install to another model is a re-init, as in the STEP
  record's "Upgrade path" (`docs/STEP-ENVELOPE-SERVICE-READERS.md`); the ledger is new, spend history stays in
  the backup.

Risks: the readers list is the risk (a missed reader is #191 again, so it needs its own enumerated-readers
record and a mutation-verified test per site); the front and seat must fail closed when the ledger's profile
is unavailable; test churn on pinned hashes. Effort **M**.

### B. Several models under one ledger (proper multi-model)

What it needs on top of A2:

- **Per-model policies under one envelope.** `price_policy()` carries a sorted list of model policies;
  `price_policy_hash` binds the set. To keep existing ledgers valid, a set of exactly one model keeps
  today's canonical encoding, so a single-model 0.91.1 ledger verifies unchanged and only a multi-model
  set uses the new form. Two canonical forms is a real complexity cost and needs a test that pins both.
- **Reservation per model.** `reserve` computes `reservation_cost_micro_eur()` once for the call
  (`:1646`, `:1682`); it becomes per model. `_assert_external_envelope` (`:462-475`) must use the largest
  reservation. `child_cap_policy` contains one `reservation_micro_eur` (`:212`) and has to become a map or
  drop the field.
- **Model selection.** Per seat is simple: the seat's `model` already drives `ANTHROPIC_MODEL`
  (`cli.py:12088-12092`) and the front already receives `request["model"]` on every call, so it can validate
  membership in the set instead of equality (`ovh_gateway_front.py:309`). Per child turn is stronger and
  needed for safety: without it a child (or a CLI sub-agent) could switch to a pricier model mid-turn and
  spend against a cheaper model's expectations. That means a `model` column on `child_turns`, a new
  `open_child_turn(model=...)` argument minted at `wrapper/run.py:2523`, front refusal on a mismatch, and a
  `CHILD_CAP_SCHEMA_VERSION` bump (`ovh_gateway.py:51`). The child-cap table shape is checked strictly
  (`:989-1015`) and migrated by `install_child_caps` (`:1048-1200`), so this is a schema migration with the
  downgrade-fence discipline that function already follows.
- **Canary per model.** Canary state is single-valued metadata (`canary_*`, `:889-940`, `:2033-2117`). It
  becomes per-model keys, and `worker_spend_ready` (`:2170-2180`) has to say which models are canary-accepted.
  A model added later needs its own canary before any seat may use it.
- **LiteLLM config** gets one `model_list` entry per model (`:2301-2341`); a set change changes the config
  hash the manifest binds, and `reconfigure` is scoped to endpoint-only changes
  (`ovh_gateway_service.py:1027-1051`), so a model-set change needs a new verb or a re-init.
- **Manifest and policy hash binding a set.** After #191 the manifest, task identity and runtime marker all
  carry the ledger's hash, so binding a set costs nothing beyond the hash itself; a display list of aliases in
  the manifest is optional.
- **Migration of a 0.91.1 ledger.** In place is unattractive: the hash sits in the ledger metadata, the
  install marker, the manifest, the task identity, the runtime marker and every historical `attempts` row,
  and the canary binds an attempt to the current hash (`:938`), so a changed hash invalidates the accepted
  canary anyway. With the single-model-keeps-its-encoding rule above, existing installs need no migration
  until they add a second model, and adding one is then a deliberate re-init. That matches the precedent the
  lead set for #191 (no in-code migration).

Risks: schema migration on a ledger that gates real spend; two canonical hash forms; per-model canary state
in `worker_spend_ready`; a larger blast radius for reviewers. Effort **L**. The account-level ceiling being
shared by all models under one ledger is the one real advantage over A2 (a single EUR envelope covers
everything), but with one gateway per host that advantage is small today.

## 2. What the wrapper and seat side needs

- **ovh-qwen profile checks**: `cli.py:12017-12019` (`cfg_agent["model"] == alias`), the supervisor
  bootstrap check `supervisor.py:8823-8824`, and the child-env builder `wrapper/run.py:490-499`. All three
  read the profile in A2 and B; in A1 they are hand edits. The launch path resolves the effective model and
  injects it as a flag, then computes a runtime fingerprint from it (`cli.py:12133-12150`).
- **Session reset**: changing the effective model changes the fingerprint and resets the wrapped session
  (the standing lesson on model and effort selection). For a clean A/B, give the cheaper model its **own
  seat name** rather than flipping an existing seat; retune only on evidence.
- **`CLAUDE_CODE_MAX_OUTPUT_TOKENS`**: pinned to `32768` (`wrapper/run.py:68`, `:493-499`) and equal to the
  gateway's max output, which the front enforces per request (`ovh_gateway_front.py:315-321`). It has to
  become the profile's own max output, and must not exceed what the model can actually generate. The
  32768 figure for the current model is itself unconfirmed (see the comment at `ovh_gateway.py:38-43`); do
  not copy it to a candidate without a source.
- **Model window**: the profile env injects only base URL, token, model and the output cap
  (`wrapper/run.py:477-482`) plus `MAX_THINKING_TOKENS=0`. There is no window or compaction setting, which
  is #194 item 1: the CLI assumes a 200K window and compacts late. A profile should carry the model's real
  window and set the CLI's compaction threshold below both that window and the request cap (section 4).
  The environment variable that controls compaction must be confirmed against the installed CLI version
  first; I am recalling one from memory and have not verified it, so this note does not name it.
- **Tool calls and reasoning**: the seat depends on reliable tool use through LiteLLM's OpenAI-to-Anthropic
  adaptation. Whether OVH serves each candidate with function calling enabled, and in a format LiteLLM
  maps correctly, is unverified. `MAX_THINKING_TOKENS=0` (`wrapper/run.py:501`) and
  `merge_reasoning_content_in_choices` (`ovh_gateway.py:2331`) exist because of a reasoning model; a
  reasoning candidate (gpt-oss-120b) exercises them harder, a non-reasoning one mostly sidesteps them.
- **Response model echo**: `settle` requires the upstream response's `model` to equal the alias
  (`ovh_gateway.py:1826-1828`), else the attempt goes `uncertain` and the gateway holds. Qwen3.8-27B works
  because the alias is the upstream id. Keep alias equal to OVH's exact model id for any candidate and
  check what the response echoes before trusting the trial.

## 3. Verifying the price and model id before any spend, and how the canary proves the ledger

Before any code or spend, from OVH's own sources (I did none of this):

1. The public catalogue page named in `POLICY_SOURCE` (`ovh_gateway.py:29`): for the candidate take the
   exact model id string, the price per 1M input and output tokens **in EUR** (the ledger prices in EUR,
   `POLICY_CURRENCY`; do not convert the third-party USD figures), the context window, the maximum output,
   and whether function calling is listed. Record the observed date as `POLICY_OBSERVED_DATE` does.
2. The OpenAI-compatible model list at the pinned base (`GET <base>/models`, base at
   `ovh_gateway_service.py:72`), with the provider key. It costs no tokens. Whether OVH serves it and what it
   lists is unverified; if it does not list the id, the catalogue id is the only source.
3. A model that answers with a different id in its response than the request (section 2) breaks settlement.
   That is visible on the very first call, so the first trial call should be a single small request through
   the gateway, watched by the operator, before any seat is started.

How the canary proves the ledger prices the model right: `canary-verify ATTEMPT_ID
--dashboard-delta-eur X` compares one settled attempt's ledger settlement (tokens times the pinned tariff)
with the OVH dashboard change the operator observes for that call; it must be nonzero and within 10% of the
ledger's figure, else a durable mismatch hold is set (`ovh_gateway.py:2033-2117`). A new model means a new
policy hash, so the canary must be redone.

One concrete risk for a cheap model: the fixture in `docs/QWEN-OVH-TRIAL.md` (1000 in / 100 out) settles to
670 micro-EUR today; on the candidate's list prices it would be about 96 micro-EUR (EUR 0.000096). If the
OVH dashboard shows spend at a coarser resolution than that, the observed delta rounds to zero and the
canary fails through no fault of the ledger. The canary attempt for a cheap model should be a deliberately
large call (for example a long input, which is what dominates the bill anyway) so the expected charge is
well above the dashboard's resolution. I do not know that resolution; it should be looked up first.
As a second check, compare the ledger's cumulative committed spend with the dashboard total after the
first trial hour.

## 4. Should #194's items ride along

| #194 item | Model-dependent? | Ride along? |
|---|---|---|
| 1. 512 KiB request cap vs the CLI compacting late (`ovh_gateway.py:63`; `ovh_gateway_front.py:294`) | Yes: the safe compaction point depends on the model's window, and the cap is roughly 128K tokens (at about 4 bytes per token, a rough figure), so it binds before a 256K window and about at a 128K one | **Yes**, the profile-env part: carry the window and a compaction threshold in the profile, and classify a gateway 413 as its own named class. Also a cost lever: 98% of spend is re-sent input, so earlier compaction cuts cost independent of the model. Treat it as a separate trial variable, since it changes quality |
| 2. One request at a time (`ovh_gateway_front.py:205`) | No | No. Separate work, size L; a cheaper model does not fix seats bouncing |
| 3. Runner exits when LiteLLM dies, no record why (`ovh_gateway_service.py` `run_service` monitor) | No, but a new model will produce new failure modes | **Yes, the small part only**: capture LiteLLM's exit status and stderr tail into the log. A supervised restart is separate |
| 4. No per-attempt wall clock; billed output above the requested maximum | Yes for reasoning models (uncapped thinking tokens) | **Partly**: record when settled output exceeds the requested `max_tokens` (S). The wall clock is separate. Note the interaction with `ovh_gateway.py:1861-1866`: a candidate with a smaller max output than 32768 makes a legitimate long reply `uncertain`, which holds the gateway, so the profile's limits must be right |

## 5. Recommendation, estimates and operator steps

| Option | Effort | What it buys | Main risk |
|---|---|---|---|
| A1 swap constants on a branch | S (about 1 day) | fastest answer on one host | off-release build, drifting literals, throwaway |
| **A2 profile at init** | **M (about 3-5 days including review rounds)** | the trial, a permanent per-install model choice, and the foundation for B | missed reader; needs an enumerated-readers record and mutation-verified tests |
| B multi-model one ledger | L (about 2-3 weeks) | several models on one host, one envelope | ledger schema migration, per-model canary, two hash forms |
| Riders (section 4: compaction knob, LiteLLM exit capture, output-over-max record) | S each | diagnostics and a cost lever for the trial | none of note |

Recommendation: A2 plus the three small riders, then the trial on the second gateway host with a dedicated
seat, first candidate `qwen3-coder-30b-a3b-instruct`. Decide on B only after the trial says the cheaper
model is worth running at all; B's extra machinery does not help answer that.

Suggested trial gate, so the decision is not vibes: same bounded card as a Qwen3.8-27B reference run, count
of tool-call failures, number of compactions and dead letters, ledger spend per completed turn.

Operator steps (re-inits are operator-run; the STEP record's "Upgrade path" in
`docs/STEP-ENVELOPE-SERVICE-READERS.md` is the one runbook and applies unchanged):

- **A1 / A2**: verify the model id and EUR prices (section 3); install the build on the trial host; stop with
  the runtime that registered the task; copy and verify the ledger backup, then move the ledger, install
  marker, old gateway state and the two token files aside; unregister the task if the interpreter changed;
  `gateway init` (A2: `--model <alias>`) with the current OVH dashboard figure; `cap-install`,
  `task-install`, `start` (read `gateway status` after a slow first start); one small watched request; the
  canary with a deliberately large attempt (attempt id from the ledger's `attempts` table, dashboard delta
  from OVH); add a new seat name whose `model` is the candidate; start it. To go back, repeat with the
  original model, which starts yet another ledger, so for A1 keep the reference model on the other host.
- **B**: one re-init to adopt the multi-model ledger, then each added model needs its own canary; adding a
  model later needs a verb that does not exist yet, else another re-init.

## Open questions and unverified statements

- Which host is the second gateway, and can it take the trial? (Inferred, not confirmed.)
- OVH's EUR prices, context windows, maximum outputs, function-calling support and exact model ids for the
  candidates. Everything about the candidates in this note other than the arithmetic is unverified.
- The OVH dashboard's spend resolution (decides whether the canary can observe a cheap call).
- The CLI's environment variable for its compaction threshold, on the installed CLI version.
- Whether the response `model` for a candidate equals the requested id (decides whether settlement works).
- The USD list prices came from the request and are a third-party index, not OVH.
