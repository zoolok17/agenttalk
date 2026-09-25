# Scope: more than one OVH model behind the qwen gateway

Status: scoping note, no code. Written against `master` at `9ce4cba0` (v0.91.1 plus the #193 docs). Line
numbers are at that commit. No spend, no gateway command other than what is read from the code; nothing
in this note was checked against OVH's own catalogue or API (section 3 says how to).

Why: the operator uses `ovh-qwen` seats as overflow capacity when the subscription allowances are tight.
Measured on Qwen3.8-27B through the gateway: roughly EUR 2-3 per busy gateway-hour, with about 98% of
tokens being input (about 77K input tokens re-sent per call, no prompt caching on OVH). A cheaper model
could cut cost several times over if quality holds; we want to trial one on one bounded card, then decide.

## Summary and recommendation

0. **Do the reasoning-effort lever first (section R).** It is config-only, applies with `gateway stop`,
   `gateway reconfigure`, `gateway start` (no re-init, no new ledger, no new canary), carries no format risk
   because the model is unchanged, and may cut both output cost and, if reasoning is re-sent as history
   (very likely under our reasoning-merge setting, testable on existing ledger data at no spend), the
   input bill. One correction: the seats pin max output at 32768 in this code, not 4096, so the 29.6K-token
   call was inside the pin.
1. Today nothing about the model is configurable: the alias, the four rates, the context and output
   limits are module constants (`ovh_gateway.py:28-44`) read at about a dozen sites, and the wrapper and
   supervisor each carry their own literal `"Qwen3.8-27B"` (`wrapper/run.py:490-492`, `supervisor.py:8823`).
   A trial on a cheaper model therefore needs code, not just a re-init.
2. Recommended for the model trial itself: **option A2**, "one model per install, chosen at `gateway init`". It is the same move
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
6. Both candidates carry tool-call format risk that our stack does not check (section 4b). Any model trial
   is gated by a cheap tiered smoke test: a local, no-spend harness extension, then about 30 scripted
   requests (about one cent), then the real CLI on a synthetic repo, then the real card.
7. Suggested first candidate, conditional on the smoke test: `qwen3-coder-30b-a3b-instruct`. It is
   non-reasoning (so no reasoning-merge behaviour and no thinking-token spend), the same family as today's model,
   and about 7x cheaper on the input-dominated mix at the quoted list prices. But the reported tool-call format
   problems (section 4b) hit exactly this model behind LiteLLM-style proxies, so it is a candidate only if it
   passes Tier 1 on OVH's deployment; if it fails, that is a cheap, decisive answer. `gpt-oss-120b` is a
   reasoning model that depends on the harmony format, with a probably smaller context window and a poor
   independent SWE-bench reproduction (as reported to me, unverified); it is a second choice, and R may well
   beat both on cost at no format risk.

## R. Reasoning effort on Qwen3.8-27B: the cheapest lever, do it first

Report to weigh: the model defaults to a very high reasoning effort and overthinks (one trivial task spent
about 22K reasoning tokens), and one call billed 29.6K output tokens. Output is priced 6.75 times input
(EUR 2.70 vs 0.40 per 1M), so a long reasoning run is the one place output cost can dominate a call.

**A correction to check first.** #194 item 4 says the seats pin max output at 4096. In this code the pin is
**32768**: `OVH_QWEN_CLAUDE_MAX_OUTPUT = "32768"` (`wrapper/run.py:68`, enforced at `:493-499`), the front's cap
is the same figure (`ovh_gateway.py:44`, `ovh_gateway_front.py:315-321`), and 0.90.0 raised it from 4096
(CHANGELOG 0.90.0). A 29.6K-token call is therefore inside the pin, not a breach of it, and "thinking tokens
are uncapped by `max_tokens`" is not needed to explain it: on OpenAI-compatible servers the completion cap
normally includes reasoning tokens. Either the 4096 figure predates 0.90.0, or that one request carried its own
smaller `max_tokens`. The ledger cannot say which, because it stores tokens but not the requested `max_tokens`
(`attempts` columns, `ovh_gateway.py:658-672`). Recording it is one of the riders in section 4.

### R.1 What the path does today (code)

- The seat child env pins `MAX_THINKING_TOKENS=0` (`wrapper/run.py:501`) and the output cap above; nothing else
  about reasoning is set. A per-seat `reasoning_effort` in supervisor config becomes `claude --effort <E>`
  (`cli.py:11169`; allowed values `low medium high xhigh max`, `supervisor.py:2451`). The ovh-qwen
  bootstrap check does not forbid it (`supervisor.py:8823-8849`), and I have not seen it set for these seats.
- The front forwards the client's JSON body unchanged, rejecting only a fixed list of routing and credential keys
  (`ovh_gateway_front.py:51-63`) and any `max_tokens` above the pin. It neither injects nor blocks a
  reasoning or `thinking` parameter.
- The LiteLLM deployment sets `extra_body: {store: false}`, `merge_reasoning_content_in_choices: true`, and,
  globally, `drop_params: true` (`ovh_gateway.py:2322-2334`). Nothing lowers reasoning, so the model runs at
  whatever default OVH's serving applies.

### R.2 Levers, cheapest first

1. **Server-side default in the LiteLLM deployment (config only).** Add a fixed parameter to the rendered
   `litellm_params` (the same place `extra_body` already lives, `ovh_gateway.py:2322-2331`). Candidate forms,
   none confirmed for OVH: a top-level `reasoning_effort` (`low`/`medium`/`high`); or a Qwen-family thinking
   switch or budget passed through `extra_body` (for example `chat_template_kwargs` with `enable_thinking`,
   a convention of common Qwen3 serving stacks). Which name OVH's endpoint accepts for this model is not in the
   repo; it has to come from OVH's model card or a probe. Two cautions. `drop_params: true` makes LiteLLM
   silently drop a top-level parameter it considers unsupported for the provider, so a probe must compare
   reasoning tokens, not just check for an error; `extra_body` is passed through verbatim but an unknown key
   may be ignored or rejected by the server. And "off" is riskier than "low" on a hybrid model: try `low` first.
   **This needs no re-init.** The price policy hash does not cover the LiteLLM config; `gateway reconfigure`
   re-renders the config from code and rebinds the manifest's config hash, preserving ledger, tokens and task,
   and its only ledger check is that the manifest's price hash still equals the ledger's
   (`ovh_gateway_service.py:1027-1090`). So: code change, then `gateway stop`, `gateway reconfigure`,
   `gateway start`. No new ledger, no new canary. Effort **S**.
2. **Client-side, seat config only.** Set the seat's `reasoning_effort` (lever exists today, no code). What the
   CLI then sends to a non-Anthropic base URL, and whether the pinned LiteLLM turns it into anything OVH honours,
   is unverified. Both can be checked with no spend: point the CLI at a local fake server and read the request
   body it sends with and without `--effort`; then run that body through the real LiteLLM against a fake
   upstream and read the translated request. The second half is exactly what
   `tests/test_ovh_litellm_conformance.py` does (opt-in via `AGENTTALK_TEST_LITELLM_EXE`; it asserts on
   `upstream.requests[0]`, `:410-424`). Changing a seat's effective effort resets its wrapped session, so use a
   dedicated seat name for the comparison.
3. **Cap output.** Lower `CLAUDE_CODE_MAX_OUTPUT_TOKENS` and `MAX_OUTPUT_TOKENS`. Crude: a hit cap truncates
   a tool call mid-argument, which is worse than long reasoning. It also changes the price policy hash
   (`ovh_gateway.py:167`, the reservation), so it is a re-init. Not recommended as the first move.
4. **Prompt-side switches** (a soft "no think" token) are not viable: the CLI's system prompt is not ours.

### R.3 Are reasoning tokens re-sent as input on later calls?

Very likely yes under our configuration, and it is worth more than the reasoning's own output cost.

- Mechanism: `merge_reasoning_content_in_choices: true` folds the model's reasoning into ordinary content
  text, so that no thinking block is ever emitted (the docstring at `ovh_gateway.py:2301-2320`; it exists
  because a thinking block arriving out of order aborted the CLI). The CLI therefore receives the reasoning as
  assistant text. The Messages protocol carries prior assistant content in every later request of the tool loop,
  so that text becomes input on each following call until the CLI compacts. This is inferred from the design, not
  measured.
- Size: reasoning of 22K tokens emitted at call k and re-sent for the next 50 calls is about 1.1M input tokens,
  EUR 0.44 at EUR 0.40 per 1M, against EUR 0.059 for producing it once. That is roughly 7 times the direct output
  cost. It also grows the request body toward the 512 KiB cap (22K tokens is on the order of 90 KB), which is
  #194 item 1's failure: reasoning inflates both the bill and the odds of a 413.
- **Test it without spend, on data we already have.** Each attempt row has `input_tokens` and `output_tokens`, and
  `child_attempts` gives the call order inside a card (agent, message id, ordinal). For consecutive calls in one
  child turn compute `input(k+1) - input(k)` and compare it with `output(k)`. If reasoning is re-sent, the
  difference tracks `output(k)` plus the tool result (slope near 1 across calls with large `output(k)`); if not, it
  tracks only the tool result. Drop compaction points (negative deltas). Run it on a verified backup copy of the
  ledger, never the live database.
- If confirmed, the levers are: produce less (R.2), or stop merging reasoning into history. The second is not a
  config switch I can point to: it would mean discarding reasoning in LiteLLM (an option I have not found) or
  rewriting the SSE stream in the front (`StreamUsage` already parses it, `ovh_gateway_front.py:103-155`, but
  today the chunks are copied verbatim). Effort **M**, with real risk. Vendor guidance for this family is
  commonly to keep earlier reasoning out of the history; that is recalled, not checked.

### R.4 Measuring the effect on cost per card

The unit is the child turn (agent, message id), which is one card message. Nothing needs to be added to measure
the baseline:

1. **Baseline first, zero spend.** On a backup copy of the ledger, per child turn: number of calls, total input,
   total output, total `actual_micro_eur`, the share of cost that is output, the largest single-call output, and
   the number of calls with output above about 8K. This sizes the lever before any change: if reasoning-heavy
   calls are a small share of spend, the lever is worth little and the model swap matters more.
2. **Protocol micro-benchmark, cents.** A fixed set of about ten prompts of varying difficulty through the real
   front with a synthetic child capability (so the spend is ledgered), default versus lowered effort, five runs
   each. Compare output tokens per call and answer quality on the easy ones. On Qwen3.8-27B this is on the
   order of EUR 0.1 to 0.3.
3. **One real card with the lever on**, compared with the baseline distribution: cost per card, output tokens
   per call at median, 90th percentile and maximum, calls per card (a lower effort can mean more steps), whether
   the card was accepted, and dead letters. Accept the lever if cost per card falls meaningfully with no drop in
   acceptance. One card is noisy; treat the baseline distribution, not a single twin card, as the comparator.
4. Riders that make this measurable going forward: store the requested `max_tokens` per attempt, and store
   reasoning tokens if the upstream usage exposes them (whether it does through this path is unverified).

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
| 4. No per-attempt wall clock; billed output above the requested maximum | Yes for reasoning models (uncapped thinking tokens) | **Partly**: record the requested `max_tokens` per attempt and when settled output exceeds it, plus a content-free request-shape record (S; needed by sections R and 4b). The wall clock is separate. Note the interaction with `ovh_gateway.py:1861-1866`: a candidate with a smaller max output than 32768 makes a legitimate long reply `uncertain`, which holds the gateway, so the profile's limits must be right |

## 4b. Detecting tool-call format failures early and cheaply

Research findings to weigh (from the request; I have not verified them): `qwen3-coder-30b-a3b` has recurring
tool-call format problems behind LiteLLM and Claude-Code-style proxies (array arguments arriving as strings, XML
tool calls that are not parsed, `read_file` loops) whose fixes needed chat-template or tool-parser patches;
`gpt-oss-120b` depends on OpenAI's harmony format, and an independent SWE-bench reproduction reported about
10% against the vendor's 62%.

What that means for our path:

- **Nothing in our stack checks tool-call structure.** The front parses only usage and the response model out of
  the stream (`ovh_gateway_front.py:103-155`) and copies the rest. LiteLLM translates OpenAI `tool_calls` to
  Anthropic `tool_use`. A model that writes its tool call as XML text arrives as plain text and no tool runs; an
  array argument sent as a string arrives as a `tool_use` whose input has the wrong type, the CLI's tool
  validation rejects it, and the model retries. Each of these burns input tokens and surfaces late, mid-card,
  looking like a dull model rather than a format fault.
- **The fix lives server-side** (OVH's serving template and tool parser), so we cannot patch it. The only
  lever is selection: reject a model whose deployment fails the checks below. Adding argument repair in the
  proxy is possible in principle but the gateway is deliberately callback-free (`ovh_gateway.py:2301`), and a
  silent repair would hide the very signal we need.
- Format checks do not measure competence. A model can emit perfect tool calls and still solve little (the
  harmony-format report is about scaffold sensitivity), so a small competence check is needed too.

A tiered gate, each tier cheaper than the next and blocking it:

**Tier 0, no spend, local.** Extend the LiteLLM conformance harness (`tests/test_ovh_litellm_conformance.py`:
the real LiteLLM against a fake OpenAI upstream, opt-in) with canned upstream replies in each malformed shape:
arguments whose array field is a JSON string, a tool call written as XML in `content`, harmony markers in
`content`, empty content with `finish_reason: stop`. Read what LiteLLM emits toward the CLI in each case. This
does not test the model; it shows which failures our stack passes through untouched and which it turns into a
hard error, so we know what Tier 1 must look for.

**Tier 1, protocol smoke test, pennies (operator-run, about 30 requests).** A short script that sends
Claude-Code-shaped requests through the real front on a synthetic, throwaway child capability, so the spend is
ledgered and the front, LiteLLM and OVH are all in the loop. Use the tool definitions the CLI actually sends
(Read, Edit, Grep, Bash, and one with an array parameter and one with a nested object). Cases: a scalar-argument
call; an array argument; a nested object; two parallel calls; a tool-result round trip followed by a final answer;
one request with about 30K tokens of context. Repeat each five times, since output is not deterministic.
Assertions per response:

- the stream parses and ends with `message_stop`;
- every `tool_use` input is valid JSON and matches the tool's `input_schema` including types (an array is an
  array, not a string), and `stop_reason` is `tool_use` when a call was expected;
- no text block contains `<tool_call>`, `<function=`, `<parameter=`, or harmony markers such as `<|channel|>`;
- no thinking block, `usage` present and nonzero, and the response `model` equals the requested alias (settlement
  requires it, `ovh_gateway.py:1826-1828`, else the attempt goes `uncertain` and the gateway holds).

Pass bar: at least 95% of cases clean. Cost at the quoted candidate list prices is about EUR 0.0003 per request,
so the whole tier is around one cent; on Qwen3.8-27B as the reference it is around EUR 0.07. Run the reference
model through the same script so a harness bug is not blamed on the candidate.

**Tier 2, real client on a synthetic repo, tens of cents.** The real Claude CLI against the gateway, on a
three-file throwaway repository, with three scripted prompts (read and summarise; edit one function; grep then
edit across two files) and a hard step cap. Automated checks: the edit applies and the tests pass, the number of
tool calls stays under a bound, and no identical tool call with identical arguments repeats more than twice (a
`read_file`-style loop detector). Run reference and candidate. Expected cost is EUR 0.05 to 0.3 per model.

**Tier 3, the real bounded card**, only after Tiers 0 to 2 pass. Give the trial host a small envelope at init
(`gateway init --cutoff-eur`, for example a few EUR): the per-turn cap defaults to the cutoff, so a runaway loop
is stopped by the ledger, not by the operator noticing (`12cc78a`; per-turn cap and cutoff, `ovh_gateway.py:541-542`). Watch `gateway status` active-turn exposure during the card.

Add to the riders in section 4: a bounded, content-free per-attempt request record (top-level keys, requested
`max_tokens`, whether a `thinking` field was present, tool count) so a format or loop problem is diagnosable
after the fact without logging prompts.

## 5. Recommendation, estimates and operator steps

| Option | Effort | What it buys | Main risk |
|---|---|---|---|
| **R reasoning-effort lever on the current model** | **S (about 1-2 days including the no-spend probes)** | the cheapest cost cut, no model risk; sizes the model swap before it is paid for | the OVH parameter name is unknown; `drop_params` can hide a dropped key |
| A1 swap constants on a branch | S (about 1 day) | fastest answer on one host | off-release build, drifting literals, throwaway |
| **A2 profile at init** | **M (about 3-5 days including review rounds)** | the trial, a permanent per-install model choice, and the foundation for B | missed reader; needs an enumerated-readers record and mutation-verified tests |
| B multi-model one ledger | L (about 2-3 weeks) | several models on one host, one envelope | ledger schema migration, per-model canary, two hash forms |
| Riders (section 4: compaction knob, LiteLLM exit capture, output-over-max record) | S each | diagnostics and a cost lever for the trial | none of note |

Recommendation: R first, with the no-spend baseline and re-send analysis (R.3, R.4), which also says how much a
model swap is still worth. Then, if it is, A2 plus the riders, then the trial on the second gateway host with a dedicated
seat, first candidate `qwen3-coder-30b-a3b-instruct` only after it passes the Tier 0-2 gate in section 4b. Decide on B only after the trial says the cheaper
model is worth running at all; B's extra machinery does not help answer that.

Suggested trial gate, so the decision is not vibes: same bounded card as a Qwen3.8-27B reference run, count
of tool-call failures, number of compactions and dead letters, ledger spend per completed turn.

Operator steps (re-inits are operator-run; the STEP record's "Upgrade path" in
`docs/STEP-ENVELOPE-SERVICE-READERS.md` is the one runbook and applies unchanged):

- **R**: after the code change ships, `gateway stop`, `gateway reconfigure`, `gateway start` (read `gateway
  status` after a slow first start); no ledger backup, no re-init, no canary; run the R.4 micro-benchmark
  through a synthetic capability; keep the previous config to revert with another `reconfigure`.
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
- Which reasoning parameter OVH accepts for Qwen3.8-27B, and what the CLI sends with and without `--effort`.
- Whether reasoning really is re-sent as input (R.3 gives the no-spend test).
- The tool-call format reports for both candidates (taken from the request, not checked), and whether OVH's
  deployments carry the parser fixes.
- The USD list prices came from the request and are a third-party index, not OVH.
