# Qwen reasoning handling on the ovh-qwen route (lever R)

Branch `feat/qwen-reasoning`, base `master` at `9ce4cba0`. Implements lever R from
`docs/SCOPE-gateway-multi-model.md` (PR #195): stop reasoning being re-sent as input, and give the route a
fixed, configurable reasoning request parameter. The model is unchanged. Everything below the operator
section was produced with no live calls and no spend: a read-only copy of the desktop ledger, a local
fake server for the Claude CLI, and the runtime's own LiteLLM against a fake OpenAI upstream.

## 1. Baseline (desktop ledger, zero spend)

Source: an SQLite online backup of the desktop ledger taken from a read-only connection on 2026-09-25,
queried as a copy. Only aggregates are recorded; turns are anonymised in first-call order. One attempt was
still `reserved` (a call in flight when the copy was taken) and is excluded. All 452 attempts carry the
current price policy, so the whole history is under the EUR 0.40 / 2.70 per 1M tariff. The VM ledger's
aggregates, supplied by the lead from a read-only query, are in a second table after the desktop figures.

| Measure | Value |
|---|---|
| Settled calls / child turns | 451 / 9 |
| Settled cost | EUR 15.29 |
| Input tokens / output tokens | 34,061,138 / 617,263 (input is 98.2% of tokens) |
| Output share of cost (at 0.40 / 2.70) | 10.9% |
| Input per call, p50 / p90 / max | 76,748 / 116,721 / 133,389 |
| Output per call, p50 / p90 / p99 / max | 506 / 3,151 / 13,985 / 18,711 |
| Calls with more than 8K output | 14 (3.1% of calls), 27.8% of output tokens, 6.6% of cost |
| Calls per turn, p50 / p90 / max | 49 / 88 / 89 |
| Cost per turn (EUR), p50 / p90 / max | 1.75 / 2.52 / 2.65 |
| Compaction-like input drops between consecutive calls | 9 of 442 |

| Turn | Calls | Input tokens | Output tokens | Cost EUR | Output share | Max output | Calls >8K |
|---|---|---|---|---|---|---|---|
| T1 | 1 | 17,814 | 74 | 0.007 | 0.027 | 74 | 0 |
| T2 | 57 | 5,261,384 | 93,003 | 2.356 | 0.107 | 12,376 | 3 |
| T3 | 35 | 2,646,044 | 41,019 | 1.169 | 0.095 | 6,845 | 0 |
| T4 | 88 | 6,228,743 | 59,464 | 2.652 | 0.061 | 6,109 | 0 |
| T5 | 46 | 3,921,393 | 143,577 | 1.956 | 0.198 | 18,711 | 6 |
| T6 | 49 | 3,947,954 | 63,635 | 1.751 | 0.098 | 7,767 | 0 |
| T7 | 51 | 3,271,358 | 92,486 | 1.558 | 0.160 | 14,203 | 3 |
| T8 | 89 | 5,820,723 | 70,835 | 2.520 | 0.076 | 8,280 | 1 |
| T9 | 35 | 2,945,725 | 53,170 | 1.322 | 0.109 | 12,301 | 1 |

**VM ledger (aggregates only, supplied by the lead).** No per-turn table and no carry-forward regression were
available for it.

| Measure | VM | Desktop |
|---|---|---|
| Settled calls | 920 | 451 |
| Settled cost | EUR 32.14 | EUR 15.29 |
| Input tokens / output tokens | 71.92M / 1,248K (input 98.3% of tokens) | 34.06M / 617K (98.2%) |
| Output share of cost (at 0.40 / 2.70, derived) | 10.5% | 10.9% |
| Input per call, p50 / p90 | 76.1K / 125.1K | 76.7K / 116.7K |
| Output per call, p50 / p90 / p99 / max | 375 / 2,811 / 14,575 / 31,709 | 506 / 3,151 / 13,985 / 18,711 |
| Calls with more than 8K output | 40 (4.3% of calls), 45.6% of output tokens | 14 (3.1%), 27.8% |

The two hosts agree on the shape: input is 98% of tokens and a call carries about 76K input at the median.
The VM has the heavier output tail: nearly half its output tokens sit in the 4% of calls above 8K, and its largest
call (31,709 tokens) is about 3% below the 32,768 output pin. That is where reasoning is most likely to
concentrate, so the VM should benefit at least as much as the desktop from the strip; its carry-forward exposure
was not computed and should be measured with the strip log after the change.

**Is output carried forward as input?** Yes, on the desktop data. Regressing the growth in input between consecutive
calls of one turn on the previous call's output tokens gives a slope of 0.957 (correlation 0.884, 433 pairs).
After a call with more than 8K of output the next call's input grew by a median 11,046 tokens against a median
10,334 tokens of output (10 pairs). If output were not carried forward the growth would track tool results only.
This does not by itself say how much of that output is reasoning, because tool-call arguments and code are
carried forward too and the ledger has no reasoning counter; that is what the new strip log is for (section 6).

**Sizing the lever.** If every output token stayed in history to the end of its turn, the output carried
forward would account for 19.4M input tokens, 57% of all input, EUR 7.78 of the EUR 15.29 spent (the compaction
drops are rare, so history mostly accumulates). Counting only the 14 calls with more than 8K output, which are
the likeliest to be reasoning-heavy, the carried-forward figure is 6.2M input tokens, 18% of input, EUR 2.48, plus
their own EUR 1.0 of output: about EUR 3.5 or 23% of the spend. The saving is the reasoning fraction of output
times these bounds, so it lies between roughly zero and 60% and is read off the strip log after the change.

## 2. Zero-spend probes

### What the Claude CLI sends (CLI 2.1.282, loopback fake server, no network)

The CLI was run with the wrapper's profile variables against a local server that recorded the request body.

| Case | `thinking` | `output_config` | other |
|---|---|---|---|
| Wrapper profile (`MAX_THINKING_TOKENS=0`) | absent | `{effort: "high"}` | `max_tokens` 32768, no `context_management` |
| Same plus `--effort low` / `--effort high` | absent | `{effort: "low"}` / `{effort: "high"}` | as above |
| `MAX_THINKING_TOKENS` unset | `{type: "adaptive", display: "omitted"}` | `{effort: "high"}` | `context_management` present |
| `MAX_THINKING_TOKENS=4096` | `{type: "adaptive", ...}` (the variable does not change it in this version) | `{effort: "high"}` | as above |

So even the pinned profile tells the endpoint `effort: high` on every request. A per-seat `reasoning_effort` does
become `--effort` and changes that field.

### What the runtime's LiteLLM does with it (LiteLLM 1.91.3, fake OpenAI upstream)

| Request or config | What reaches the upstream |
|---|---|
| Any of `output_config.effort` (high or low), `thinking` adaptive, `thinking` disabled, a top-level `reasoning_effort` | nothing: only `max_tokens` and `store` (all of these are silently dropped) |
| `thinking` `{type: "enabled", budget_tokens}` | a Responses-API-shaped body (`input`, `max_output_tokens`) instead of chat completions. Not sent by the pinned profile, but a hazard if a profile ever enables thinking |
| `reasoning_effort` as a top-level `litellm_params` key | dropped (`drop_params: true`) |
| `reasoning_effort` and `chat_template_kwargs` under `extra_body` | passed through verbatim |

Consequences: the CLI's effort field is a dead lever on this route, top-level LiteLLM params are a trap, and
`extra_body` is the one mechanism that works. Responses, with reasoning in the upstream stream:

| Config | Client-visible stream |
|---|---|
| `merge_reasoning_content_in_choices: true` (previous) | reasoning and answer share one `text` block: reasoning is ordinary assistant text, so the CLI re-sends it |
| merge off (new) | separate typed `thinking` blocks, interleaved with text and tool_use, indices consecutive, usage unchanged |

### Idle watchdogs in the CLI

Strings in the CLI bundle show `CLAUDE_STREAM_IDLE_TIMEOUT_MS` (effective idle timeout at least five minutes) and
`CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS` (three minutes first-party, otherwise the five-minute figure, clamped
between ten seconds and thirty minutes), with `CLAUDE_ENABLE_STREAM_WATCHDOG` and `CLAUDE_ENABLE_BYTE_WATCHDOG`
switches. Whether they are on by default was not determined. The field saw a call stream for over seven minutes,
so removing reasoning bytes from the stream would create dead air; the strip therefore emits Anthropic `ping`
events (section 3). The wrapper's own liveness is unaffected: it stamps on `thinking_delta` but not on
`text_delta`, and merged reasoning already arrived as text, so a silent reasoning stretch looks the same to the
wrapper before and after.

## 3. The change

**(a) Reasoning is stripped before the CLI sees it.**

- `render_litellm_config` no longer sets `merge_reasoning_content_in_choices`. LiteLLM emits typed `thinking`
  blocks.
- `ovh_gateway_reasoning.SseReasoningStripper`, run by the front on the response, drops `thinking` and
  `redacted_thinking` blocks, every `thinking_delta` and `signature_delta` (even one that lands on a text block,
  the historical out-of-order abort), and renumbers the remaining blocks so indices stay consecutive. Everything else,
  including `message_start`, `message_delta` (usage) and `message_stop`, is forwarded byte-for-byte. It is
  chunk-boundary independent, accepts LF and CRLF framing, and fails open: an unparsable event is forwarded, and
  an event over 1 MiB switches the rest of the stream to raw passthrough.
- While events are being dropped and nothing has been forwarded for 15 seconds, a standard `ping` event is
  emitted so the client never sees dead air. The real CLI was run against a stream with pings and renumbered
  blocks and completed normally.
- Usage is read from the unfiltered upstream bytes, so the ledger still settles on the provider's real,
  reasoning-inclusive token counts. Reasoning is still paid for once as output; it is no longer paid for again
  as input on every later call.
- Non-streaming replies are buffered (up to 8 MiB) and stripped the same way.
- The front writes `reasoning-stripped.jsonl` in the gateway state directory: attempt id, block count and
  character count per attempt, no content, rotated at 1 MiB. Divide characters by about four for tokens.
  `FrontConfig.strip_reasoning` (default on) switches stripping off.

**(b) A fixed, configurable reasoning parameter.** `gateway init` and `gateway reconfigure` take
`--reasoning-param NAME=VALUE` (repeatable); `reconfigure --no-reasoning-param` removes them. They are rendered
under the deployment's `extra_body`, recorded in the install manifest (`reasoning_params`), shown by
`gateway status`, and re-validated on every manifest load. Default: none, and the rendered `extra_body` is
exactly `store: false`. The grammar is closed so the setting cannot become a routing or accounting lever: NAME is
lowercase letters, digits and underscores, optionally prefixed `chat_template_kwargs.`; VALUE is `true`,
`false`, an integer of at most seven digits, or a short lowercase word; at most eight parameters; names that
would change what is sent or how it is billed (`model`, `messages`, `tools`, `stream`, `store`, `max_tokens`,
`api_base`, `extra_body`, and so on) are refused. String values are rendered quoted so YAML cannot coerce
them. The parameters are not part of the price policy, so changing them needs neither a re-init nor a new canary.

### Enumerated readers and writers of reasoning content

| Site | Role | Now |
|---|---|---|
| `ovh_gateway.py` `render_litellm_config` | wrote the merge fold | fold removed; renders `reasoning_params` under `extra_body` |
| `ovh_gateway_front.py` `_proxy` | forwarded response bytes verbatim | strips reasoning (SSE and JSON), pings, settles on unfiltered usage |
| `ovh_gateway_front.py` `StreamUsage` | reads usage | unchanged, fed the unfiltered bytes |
| `ovh_gateway_service.py` `initialize_install`, `reconfigure_endpoint`, `_validate_install_manifest`, `gateway_status`, `run_service` | config, manifest, status, front construction | carry `reasoning_params`; `run_service` wires the strip log |
| `cli.py` `gateway init` / `reconfigure` | operator interface | new flags |
| `wrapper/claude_adapter.py` | reads `thinking_delta` as liveness | unaffected: the CLI now never receives thinking blocks, and merged reasoning never stamped liveness either |
| ledger (`ovh_gateway.py`) | settles on usage | unchanged; the price policy hash is unchanged |

## 4. Operator steps

Applying it needs no re-init, no ledger backup and no new canary: the price policy hash is unchanged, so the
accepted canary stays valid. The task interpreter must stay the same (upgrade the package in place in the
runtime that registered the task), otherwise follow the unregister step in
`docs/STEP-ENVELOPE-SERVICE-READERS.md`.

1. Take the "before" measurement for the card (section 6).
2. `agenttalk gateway stop` and confirm both loopback ports are free.
3. Install the release that contains this change into the same runtime.
4. `agenttalk gateway reconfigure` (add `--reasoning-param NAME=VALUE` once the probe in section 5 has found the
   parameter). This re-renders the config without the merge fold, rebinds the manifest's config hash, and touches
   neither ledger, tokens nor task. `changed` should be `true`.
5. `agenttalk gateway start`. After a slow first start read `gateway status` before retrying (see the upgrade doc).
6. `agenttalk gateway status`: `ready` and `worker_spend_ready` true (the old canary still applies),
   `config_sha256` changed, and `reasoning_params` as expected.
7. Run one seat turn and check that `.agenttalk/gateway/reasoning-stripped.jsonl` gains a line per attempt that
   reasoned. No lines means either the model did not reason or LiteLLM did not emit thinking blocks: treat that as a
   finding, not success.

Rollback: `agenttalk gateway stop`, reinstall the previous release, `agenttalk gateway reconfigure`,
`agenttalk gateway start`. The previous release re-renders the merge fold and ignores the extra manifest key.

## 5. Finding the OVH reasoning parameter (one bounded probe)

The parameter OVH accepts for Qwen3.8-27B is not in the repository. Find it before setting anything, with
one small direct request per candidate to OVH's chat-completions endpoint using the provider key (outside the
gateway and so outside the ledger, but bounded: `max_tokens` 2048 is at most about EUR 0.006 per request at the
output tariff). Use a trivial, reasoning-prone prompt such as "What is 17 times 23? Answer with just the number."
and send four requests: no extra parameter, then `reasoning_effort: "low"`, then
`chat_template_kwargs: {"enable_thinking": false}`, then `reasoning_effort: "medium"`. Read the JSON reply, not
the stream: `usage.completion_tokens`, `usage.completion_tokens_details.reasoning_tokens` when present, and the
length of `choices[0].message.reasoning_content` (or `reasoning`) when present.

- A parameter that works: completion tokens fall sharply against the baseline and the answer is still right.
- A parameter OVH ignores: no change against the baseline.
- A parameter OVH rejects: an HTTP 400 naming the field. Do not configure it.

Prefer the lowest setting that removes the overthinking over turning reasoning off. Then set it with
`gateway reconfigure --reasoning-param ...` and confirm with the strip log that reasoning per attempt fell. Do
not configure a parameter as a top-level LiteLLM key: it is dropped silently (section 2).

## 6. Measurement protocol: one bounded card before and after

Run on a verified backup copy of the ledger (SQLite's online backup from a read-only connection), never the live
database. Record the settled-attempt high-water mark (latest `admitted_at`) before each card, then aggregate only
the attempts after it:

```sql
SELECT ca.agent, ca.message_id,
       COUNT(*) AS calls,
       SUM(a.input_tokens) AS input_tokens,
       SUM(a.output_tokens) AS output_tokens,
       ROUND(SUM(a.actual_micro_eur) / 1e6, 4) AS cost_eur,
       ROUND(SUM(a.output_tokens * 2.70) / SUM(a.output_tokens * 2.70 + a.input_tokens * 0.40), 3) AS output_cost_share,
       MAX(a.output_tokens) AS max_output,
       SUM(a.output_tokens > 8000) AS calls_over_8k
FROM attempts a JOIN child_attempts ca ON ca.attempt_id = a.attempt_id
WHERE a.state = 'settled'
GROUP BY ca.agent, ca.message_id
ORDER BY MIN(a.admitted_at);
```

Compare, for the "before" card (stripping off, that is the previous release) and the "after" card: cost per card,
calls per card (a lower effort can mean more steps), input per call at the median and 90th percentile, output per
call at the median, 90th percentile and maximum, and whether the card was accepted. One pair of cards is noisy, so
also compare against the baseline distribution in section 1 (cost per turn EUR 1.75 median, 2.52 at the 90th
percentile). The strip log gives the reasoning volume directly: characters divided by four, per attempt.

## 7. Verification

- `tests/test_ovh_gateway_reasoning.py`: the parameter grammar (accepted forms, reserved names, injection,
  duplicates, limits), the exact default config, rendered parameters and YAML round-trip, the stripper on the
  real LiteLLM stream shapes (tool call, interleaved, out-of-order delta, redacted, identity when no reasoning,
  every chunk boundary, CRLF, fail-open, ping cadence), JSON stripping and the strip log.
- `tests/test_ovh_gateway_front.py`: the front strips but settles on the provider usage; passthrough when
  disabled; JSON replies; an observer that raises cannot affect settlement.
- `tests/test_ovh_gateway_service.py`, `tests/test_ovh_gateway_cli.py`: init, reconfigure keep, replace and clear,
  tampered manifest fails closed, status field, CLI flags and exclusivity.
- `tests/test_ovh_litellm_reasoning_conformance.py` (opt-in, `AGENTTALK_TEST_LITELLM_EXE`): the runtime's real
  LiteLLM behind the stripper, and the request-side findings in section 2. Run against LiteLLM 1.91.3 for this
  change; it should be rerun whenever the LiteLLM runtime is upgraded.
- Mutation-verified: reverting each of thinking-block removal, index renumbering, thinking-delta removal, fail-open,
  ping emission, JSON stripping, front stripping (stream and JSON), observer isolation, the merge fold, `extra_body`
  placement, the reserved-name check, stored-parameter retention and the manifest validation makes a new test fail.

## 8. Limits and what was not verified

- Live behaviour with OVH's real stream was not tested (no live calls). The stripper is built on LiteLLM's
  actual output for OpenAI-style `reasoning_content`; if OVH's server returns reasoning inline as `<think>` text in
  `content` instead, neither the old fold nor this strip touches it, and the strip log stays empty (step 7).
- The CLI's idle watchdogs were found in the bundle but their defaults were not determined; pings are a
  precaution. The CLI accepted a stream with pings in a loopback run.
- The size of the saving depends on the reasoning share of output, which the ledger cannot show; section 1 gives the
  bounds and the strip log will give the figure.
- The VM baseline is aggregates only; its per-turn shape and carry-forward slope were not computed.
