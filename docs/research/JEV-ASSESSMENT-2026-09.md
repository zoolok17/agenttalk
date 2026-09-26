# Assessment: TypeSafe AI's "Jev" decision model for agenttalk and Leasewise

Date: 2026-09-26. Status: desk research only. No signup, no API call, no code change, no data left this machine.
Method note: pages were read through a fetch tool that summarises them, so every load-bearing fact below was taken from
a primary TypeSafe page where one exists and is marked **[P]** (primary) or **[S]** (secondary or third-party,
not confirmed by TypeSafe). Where a fact could not be found it is listed under "Not found" in section 1.10 rather than guessed.

## 0. Bottom line in five lines

1. Jev is a hosted classifier-style API that returns calibrated probabilities, never text. It is cheap and fast, and its own
   documentation lists real weaknesses (numbers, dates, hostile text, large irrelevant state).
2. It fits agenttalk only as an **advisory annotator** beside the typed rails. It must never feed a gate, a status or a
   verdict, which keeps prose from moving state.
3. The content it needs is exactly the content the standing no-client-code-egress rule protects, so anything that sends
   real bus or project text to TypeSafe needs the operator's explicit yes. Synthetic data needs none.
4. A local or self-trained alternative exists (open Kev, or a fine-tune on a small base), so the first pilot should compare
   Jev with a rule baseline and a local model, not with nothing.
5. Recommendation in section 5: a small, offline, synthetic-only pilot on one use case ("reply prose contradicts its typed
   status"), no production dependency.

## 1. What Jev is (sourced)

### 1.1 Concept
- A "System One model": it "does not generate text, write code, or hold a conversation"; it takes a state and typed
  questions and returns structured answers. [P] <https://docs.typesafe.ai/introduction/coding-agents>
- Three answer types: a `choice` from a list with per-option probabilities, a `score` on a rubric you define, and a
  `noul` (0-1) for a true/false statement. [P] same page. The lead's first read is correct; the true/false primitive is
  called "noul" in the API.
- Trained with "Reinforcement Learning for Calibrated Decisions (RLCD)" on "a new model architecture, parallel sampler";
  it "generates all outputs in a single query" rather than token by token. [P]
  <https://typesafe.ai/blog/introducing-system-one-models-and-jev>
- Current model `jev-1.13.0`; aliases `jev-latest` and `jev-preview` both point at it today. [P]
  <https://docs.typesafe.ai/models.md>

### 1.2 API shape [P] <https://docs.typesafe.ai/api.md>, <https://docs.typesafe.ai/introduction/quickstart.md>
- `POST https://api.typesafe.ai/v1/systemone`, header `Authorization: Bearer <API_KEY>`, JSON body.
- Request fields: `state` (string, object or array of text), `model` (for example `"jev-latest"`), `questions`
  (a map of named question objects with `type`, `instructions`, optional `criteria`).
- `noul`: optional `criteria` with `true` and `false` descriptions; answer `noul` in 0-1.
- `choice`: `criteria` map of options, at most 255; answer `choice`, `probabilities`, `confidence`.
- `score`: `criteria` array of 2-10 levels; answer `score`, `legend`, `probabilities`, `confidence`.
- Response: `{"model", "answers": {<question id>: ...}, "usage": {"input_tokens", "output_tokens"}}`.
- Errors: 401, 422 (malformed question), 429 (rate limit), 529 (overloaded); back off on 429/529.
- SDKs: Python (`pip install typesafe-sdk`, Python 3.10 or later, reads `TYPESAFE_API_KEY`) and JavaScript. [P]
- State guidance: "use an object for most requests so each part of the state has a descriptive name"; text only, no
  images, audio or video; English is where accuracy is best, other languages "have lower accuracy". [P]
  <https://docs.typesafe.ai/concepts/state.md>, models page.

### 1.3 Confidence [P] <https://docs.typesafe.ai/confidence.md>
- Every `choice` and `score` answer carries `confidence` in 0-1, derived from the probability spread. TypeSafe's own
  advice: above 0.9 "act automatically", below 0.5 "do not act, route to a human", and "a confidence threshold is not one
  number"; gate each action by the cost of being wrong. It publishes no calibration figures: "test with your own data".
- A `noul` is the probability that the answer is yes; "it's not a scale of the thing you asked about".
  <https://docs.typesafe.ai/primitives/noul.md>

### 1.4 Limits [P] <https://docs.typesafe.ai/models.md>
- 64k tokens per request, of which 32k is the budget for the `state` plus the longest question.
- 250,000 tokens per second and 1,200 requests per minute per account, and "the limits above can change without notice".
- [S] Cloudflare's model page lists a 32,000-token context (<https://developers.cloudflare.com/ai/models/typesafe/jev/>),
  which agrees with the 32k state budget rather than the 64k total.

### 1.5 Latency
- Vendor: "end-to-end response time is 70ms-500ms" and "40x-200x faster than frontier LLMs". [P] launch blog.
- Independent, small: with 8 requests in flight, Jev's median was about 275 ms, mostly network. [S]
  <https://opper.ai/blog/jev-vs-kev-open-decision-model>. No latency SLA was found.

### 1.6 Pricing
- Input $0.042 per million tokens ($42 per billion), output free. [P] launch blog, models page, homepage.
- [S] Opper measured "a fixed charge of about 257 input tokens on every request", so a one-line message plus one question
  is about 12 times dearer than the raw text suggests; a long message with five questions about 1.3 times.
  <https://opper.ai/blog/jev-vs-kev-open-decision-model>. Treat 257 as an estimate to re-measure.
- [S] Aggregator sites report a $5 free credit; not on any TypeSafe page read.

### 1.7 Access status
- Homepage: "Try our first System One Model, Jev, in early access". Launch date 2026-09-15 matches the lead's note.
  [P] <https://typesafe.ai>. The quickstart says only "get your API key from the dashboard"
  (<https://console.typesafe.ai/keys>) and mentions no waitlist.
- [S] Several aggregator posts say the waitlist was dropped on 2026-09-21 and anyone can sign up; none is TypeSafe's own
  statement, and whether this is GA with an SLA is unstated. Verify in the console before relying on it.

### 1.8 Data handling and retention [P] <https://docs.typesafe.ai/legal.md> and the pages it links
- Privacy policy: "We will not train or fine tune any artificial intelligence or machine learning models on your
  prompts or other Input." Input (prompts, data, instructions) is collected as personal data; retained "for as long as
  reasonably necessary ... or otherwise in support of our business or commercial purposes", with no time frame; deletion on
  request; "the Services are hosted in the United States"; vendors and service providers may receive data (Google
  Analytics is named). <https://typesafe.ai/legal/privacy-policy>
- Data Processing Agreement: retention "as long as necessary" for the purpose; 72-hour breach notice; 15 days' notice of
  new subprocessors (list at <https://trust.typesafe.ai/subprocessors>); "no zero-retention terms specified". Hosting
  region and logging practice are not stated in it. <https://typesafe.ai/legal/data-processing>
- Zero data retention (ZDR) is offered to enterprise customers through sales (`sales@typesafe.ai`), not self-serve. Models
  page: "not trained on customer requests/responses".
- [S] Cloudflare's page states "zero data retention" for the model as offered there; that is Cloudflare's channel and
  terms, not TypeSafe's, and it is unclear whether Cloudflare runs the model or proxies to TypeSafe.
- Net: on TypeSafe's direct API the prompts leave the machine, reach a US-hosted service, and are retained for an
  open-ended "reasonably necessary" period unless an enterprise ZDR arrangement exists. Not trained on, but not ephemeral.

### 1.9 Self-hosting and training your own
- No page read says Jev's weights are released; TypeSafe presents a hosted API only. [P, by absence]
- Together's post trains a "Jev-like" classifier on **Qwen3.5 4B**: 37,840 examples from eight sources (MultiNLI 5,000,
  BoolQ 3,000, Banking77 3,000, AG News 1,500, SST-5 2,000, programmatic policies 13,500, routing 6,000, research taxonomy
  3,840), about $17 and roughly 25 minutes of fine-tuning on Together, deployed as a dedicated endpoint (one H100 80 GB in
  the example). It publishes no accuracy figures. [P-third-party]
  <https://www.together.ai/blog/how-to-train-your-own-jev>. That is a lookalike you host on Together, not Jev.
- **Kev** is an independent Apache-2.0 open implementation of the same `/v1/systemone` contract on Qwen3.5 (0.8B, 4B, 9B,
  LoRA plus a pointer head), runnable with Ollama or vLLM. Its repository shows 15 stars and 15 commits and says it is "not
  affiliated with TypeSafe AI". [S] <https://github.com/arjun988/Kev>
- Opper's small side-by-side (362 items published after 2026-09-20, three easy tasks) found Jev and Kev within a few points
  on accuracy (Jev 95-97.5%, Kev 94-98%) with Jev better calibrated (calibration error 0.03-0.05 against 0.04-0.14), and
  noted differences under about 5 points are noise and Kev was trained on short states (up to 384 tokens). [S]
  <https://opper.ai/blog/jev-vs-kev-open-decision-model>

### 1.10 Documented weaknesses and other reports
- TypeSafe's own "jaggedness" page for Jev 1.13 lists: it answers the question you wrote, not the one you meant; "not a
  calculator" and "does not count reliably"; numeric forms such as hex or RGB values; dates read "as text, not as ordered
  quantities"; double negatives and indirection; "accuracy falls as the state grows with content unrelated to the
  decision"; adversarial content is not "treated as hostile by default" so injected instructions can move answers;
  conflicting instructions and criteria confuse it; no guarantee that complementary questions sum to 1; it is "not trained
  to generate text". [P] <https://docs.typesafe.ai/model-jaggedness/jev-1.13.md>
- Simon Willison: input $0.042 per million, output free, "currently not great with numbers, dates, or adversarial
  content", a black box that returns only numbers, and a bias example where scoring Bay Area cities as "good" ranked one
  city highest and another lowest. [S] <https://simonwillison.net/2026/Sep/21/jev/>
- TypeSafe's launch blog itself says its demo workflows "were made by individuals on our model capabilities team, so some
  bias could exist", limits choice cardinality to 255, and leaves public benchmarks and "is it just a smaller LLM" as open
  FAQ questions. [P]
- LangChain shows two harness uses, routing to "the least costly model that can complete the task" and an
  `AutoModeMiddleware` that checks tool calls for risk before they run, and publishes no evaluation numbers. [S]
  <https://www.langchain.com/blog/building-a-harness-with-jev>

**Not found** in the sources read: a data-retention time limit; hosting region beyond "United States"; the subprocessor
list contents; a latency or availability SLA; a published calibration or accuracy benchmark from TypeSafe; a statement on
weight release; pricing for enterprise or ZDR.

## 2. Candidate uses, ranked by value and fit

Common design rules for every use below (they follow agenttalk's existing pattern for advisory read-only projections such
as `attention.py`, `capacity.py` and `coordination_stall.py`):
- **Advisory only.** The output is a derived annotation in a sidecar or a row in the operator view. It creates no message
  kind, changes no status, and is never read by `gates.py`, `close.py`, `acceptance*.py` or any verdict path. A test should
  assert those modules do not import the Jev adapter. Prose still never moves state.
- **Pinned model** (`jev-1.13.0`, never `jev-latest`), every answer stored with model id, question text hash, input hash and
  confidence, so results are reproducible and a model change is visible.
- **Low confidence means no annotation.** Below the per-use threshold, or on any error, timeout, 429 or 529, the system
  behaves exactly as it does today. Failing closed here means "say nothing".
- **Egress class check before any call** (section 3): a per-project flag decides whether a call may be made at all.

| Rank | Use | Value | Fit | Egress exposure | Verdict |
|---|---|---|---|---|---|
| 1 | Reply prose contradicts its typed status | high (a known failure class) | high: labels come free | medium: reply bodies | pilot first |
| 2 | "Needs a human?" triage of untyped bus traffic | high | good: labels from operator dispositions | medium-high | second |
| 3 | Work order self-containedness check | medium | fair: much is deterministic | medium-high | third |
| 4 | Ranking lessons for context packs | medium-low | fair | low if only metadata is sent | fourth |
| 5 | Turn routing to a model tier or account (Leasewise) | high in cost terms | risky | high | not yet, local first |
| 6 | Stuck versus busy hints | low | poor: numbers and durations | medium | not recommended |

### 2.1 Reply prose contradicts its typed status (recommended first)
- **Decision:** should the operator or lead look twice at a reply because the text says one thing and the typed status says
  another? Existing failures of this kind (a reply that reads as done while the typed status is something else, or the
  reverse) are the reason typed status exists; this is a second, fuzzier net.
- **Typed question:** `noul`, "The reply text reports the work as finished, but the typed status says it is not, or the
  reverse", with `criteria` `true`: "the text asserts an outcome (done, declined, blocked, partial) that differs from the
  status", `false`: "the text and the status agree, or the text makes no claim about outcome".
- **State (object):** `{"typed_status": "<status>", "kind": "<kind>", "reply_text": "<first N characters>"}`; no task body,
  no repository content, no paths.
- **Low confidence:** a `noul` between roughly 0.3 and 0.7 is treated as no signal. Above a high threshold (chosen from the
  pilot's reliability table, not from TypeSafe's generic 0.9) it adds an advisory row to the attention view ("reply text and
  status may disagree, look before relying"). It never edits the reply or the status.
- **Gate contact:** none. It cannot clear or set HOLD, and a flagged reply is still judged by its typed fields alone.
- **Why first:** ground truth is manufacturable: take real reply and status pairs, and build labelled contradictions by
  swapping the status or editing the outcome sentence. Numbers and dates, Jev's weak spot, are mostly absent.

### 2.2 "Needs a human?" triage
- **Decision:** among messages that carry no typed `needs_operator` marker, which ones deserve a place in the small operator
  queue? The typed marker path stays authoritative; this only ranks the untyped remainder.
- **Typed question:** `choice` over `{routine, informational_for_operator, likely_needs_operator}` with a description per
  option, plus a `noul` "asks the operator for a decision or approval".
- **State:** kind, thread relation, subject and a bounded excerpt of the body.
- **Low confidence:** below threshold, no effect; the item is not added. Above it, it appears ranked below every typed
  item, labelled as inferred.
- **Gate contact:** none; the queue is read-only and its dispositions annotate attention only (per `attention.py`).
- **Cautions:** adversarial content (an agent quoting text that says "escalate now") is a documented weakness; the operator
  queue must not be floodable by a hostile string, so it ranks below typed items and is rate-limited. Labels exist (the
  durable operator dispositions), but they are few and biased toward what already got escalated.

### 2.3 Work order self-containedness
- **Decision:** before a lead dispatches a task, does the order state the repository or branch, the acceptance criteria, the
  reply form, and the guardrails, so a cold reader could act?
- **Typed question:** several `noul` questions, one per required element, for example "states which repository and base
  branch to use", "states what done means".
- **State:** the order text (this is project content, often quoting paths and identifiers).
- **Low confidence:** a missing element is only a hint to the sender; nothing is blocked.
- **Gate contact:** none, and it must not become a dispatch precondition.
- **Fit:** the required elements are mostly checkable by a deterministic template, which should be tried first; Jev adds
  value only for fuzzy cases (a criterion that is present but vague), and "answers the question you wrote, not the one you
  meant" applies.

### 2.4 Ranking lessons and context for context packs
- **Decision:** which accepted lessons to surface for a turn. Today `lesson_context.py` selects and ranks by declared
  context scope; a model would re-rank within a scope.
- **Typed question:** `score` per lesson (for example 0-3 relevance) against the turn's context, or a `choice` of the top
  scopes.
- **State:** lesson text (process guidance, low sensitivity) and the turn context. To keep exposure low the context can be
  limited to kind, subject and scope tags, not the body.
- **Low confidence:** fall back to the current deterministic ranking.
- **Gate contact:** none; lessons are advisory memory by definition.
- **Fit:** a few dozen lessons and existing scope matching leave little room for gain; the real risk is silent ranking
  drift that hides a relevant lesson. Measure against the current ranker before considering it.

### 2.5 Turn routing to a model tier or account (Leasewise)
- **Decision:** which model tier or account should serve the next turn, given budgets.
- **Typed question:** `choice` over tiers, or a `score` for task difficulty, then a deterministic budget policy picks the
  account. This mirrors LangChain's "least costly model that can complete the task". [S]
- **State:** ideally derived features only (tool count, files touched, prior failure count, retry, token estimate), not the
  prompt.
- **Low confidence:** route to the configured default (the stronger tier). A wrong cheap route costs quality; a wrong dear
  route costs money; the asymmetry decides the default.
- **Gate contact:** none for agenttalk's gates; in Leasewise it would change spend, so it needs its own guardrails and a
  budget ceiling that Jev cannot override.
- **Cautions:** sending the prompt defeats the point for private work; derived features are numeric, which is Jev's weak
  area; a small local classifier trained on Leasewise's own routing outcomes is likely better. Not before a local-first
  design exists.

### 2.6 Stuck versus busy hints
- **Decision:** is a wrapped seat wedged or working?
- **Fit: poor.** The real signals are timestamps, heartbeats, elapsed time and tool state, which are numbers and durations
  handled by deterministic thresholds; Jev reads dates as text and does not count reliably. The existing
  advisory `coordination_stall` and supervisor heartbeat logic are the right basis. Not recommended.

## 3. Risks

1. **Sending project content to a third party.** Every use above except a metadata-only variant of 2.4 and 2.5 sends text
   that agents wrote about client work, sometimes with code, identifiers or paths. Under the standing no-client-code-egress
   rule that is prohibited for those projects. TypeSafe's terms give no training on Input but open-ended "reasonably necessary"
   retention, US hosting and named subprocessors; ZDR is enterprise-only. Controls: (a) a per-project egress class checked
   before any call, default deny; (b) synthetic or scrubbed inputs only until the operator says otherwise;
   (c) `redaction.py` removes secret-shaped strings but is not a code or identifier scrubber, so it is not a sufficient
   control on its own; (d) the API key lives outside the repo like the gateway's keys; (e) prefer a local model for anything
   touching real content.
2. **Early-access dependency.** "Early access" per the homepage, limits that "can change without notice", one small vendor,
   a moving alias (`jev-latest`), no SLA found. Mitigation: pin the model id, keep every use optional and non-blocking, keep
   the deterministic path complete, and design the adapter around the open `/v1/systemone` contract so Kev or a self-trained
   model can be swapped in.
3. **Numbers, dates and adversarial text.** All three are named weaknesses, and bus content routinely contains numbers,
   timestamps and quoted text from elsewhere. Uses that hinge on them (2.6, parts of 2.5) are poor fits; the others need an
   injection test set in the eval.
4. **Bias.** Both Willison's example and TypeSafe's own disclaimer say scores can carry bias. For triage this could
   systematically demote messages from certain agents, projects or writing styles; the eval should slice results by sender
   and kind.
5. **No explanations.** A float cannot be audited. For an advisory flag that is acceptable only if the operator can see the
   input that produced it and can dismiss it; the annotation must carry its inputs' hash and the question so a human can
   re-read it.
6. **Eval burden.** TypeSafe publishes no calibration numbers and tells users to test on their own data, so all evidence
   must come from us: labels, a rule baseline, a local model, an injection suite, calibration checks, and a re-run on every
   model version. Small-sample independent results so far are on easy public tasks and are within noise.
7. **Operational.** Latency of 70-500 ms is fine for advisory work but should not sit on any hot path; retries and
   timeouts must never delay message delivery or a turn.

## 4. A small, zero-risk pilot

**Use case:** 2.1, reply prose versus typed status. Purely advisory, labels manufacturable, little numeric content.

**Phase 0, local only (no operator decision needed).** Build the dataset from our own recorded bus data kept on this
machine, plus fully synthetic replies. Construct pairs: real (status, reply) pairs assumed consistent after a manual audit of
a random sample (say 100, by an agent and one human spot check), and negatives made by swapping the status or editing the
outcome sentence. Add hand-written hard cases: negation ("not done"), partial results, numbers ("3 of 5 passed" with a done
status), dates, quoted text from other messages, and an injection set (reply text that says "ignore the status"). Score three
systems entirely locally: a rule baseline (keywords plus status), and Kev 4B/9B or a small local model through Ollama with
logprob-derived probabilities. Nothing leaves the machine.

**Phase 1, Jev on synthetic data only.** With an operator-created key, run only the synthetic and the hand-written cases
through `jev-1.13.0` (pinned). Real replies are not sent. This tests Jev's behaviour and calibration on our task shape at
almost no cost and no privacy exposure.

**Phase 2, only with the operator's explicit yes.** A scrubbed sample of real replies, after the operator decides which
projects and which fields may leave the machine, ideally under a written ZDR or equivalent arrangement.

**Success metrics** (set before running; a miss on any is a stop):
- Precision of the flag at or above 90% at recall of at least 50% on the held-out set, beating the rule baseline by a
  clear margin (for example halving its false alarms at equal recall).
- Calibration: reliability table with expected calibration error at or below 0.05 on held-out data, and a usable confidence
  threshold that is stable across two model-pinned reruns.
- Injection set: at most 5% of injected replies flip the answer.
- No slice (by sender or kind) with precision more than 10 points below the overall figure.
- Latency p95 under 500 ms from this network; error rate under 1%.
- The advisory row, when shown, is dismissed as useful by a human in at least 3 of 4 spot checks.

**Cost estimate.** Input is $0.042 per million tokens with free output; assume 800 tokens per item including the roughly
257-token fixed overhead. 5,000 items are about 4 million tokens, about $0.17; 50,000 items about $1.70. Even production
volume of a million messages at 1,000 tokens each is about $42. The real cost is engineering (an adapter, an egress check,
the eval harness: about a week for Phase 0 and 1) and human labelling time, not the API bill.

**What the operator must do.**
1. Decide the data class: synthetic only (recommended now), scrubbed real data, or nothing. Default is nothing real.
2. If Phase 1 is wanted: create a TypeSafe console account and API key (accepting their terms, which include the
   privacy policy and DPA), and store the key outside the repository as `TYPESAFE_API_KEY`, never in a message or config.
3. If Phase 2 is ever wanted: decide per project whether egress is allowed and whether an enterprise ZDR arrangement is
   required first.
4. Approve or reject Phase 0's use of recorded local bus data (it stays on this machine).

## 5. Recommendation

Do not adopt Jev as a dependency now, and do not let it near a gate, a status or a verdict; agenttalk's value is that typed
evidence, not prose or a float, moves state. It is, however, cheap enough and well enough documented to justify a narrow
offline pilot of one advisory annotator, "reply text disagrees with its typed status", run first entirely locally against a rule
baseline and an open local model (Kev or a small self-trained Qwen classifier), then against Jev on synthetic data only, with real content
sent to TypeSafe only on the operator's explicit, per-project yes, because its terms retain input for an open-ended "reasonably necessary"
period on a US-hosted service and the standing no-client-code-egress rule forbids that for client work. Keep the adapter on
the open `/v1/systemone` contract so the vendor is replaceable, pin `jev-1.13.0`, and treat the pilot as a stop-on-miss
experiment. Defer Leasewise routing until a local-first design exists, and skip stuck-versus-busy entirely. Revisit in
about a month for GA status, retention terms and independent benchmarks.

## 6. Sources
- TypeSafe docs: introduction/coding-agents, api.md, models.md, legal.md, confidence.md, primitives/noul.md, concepts/state.md,
  model-jaggedness/jev-1.13.md, patterns/confidence-routing.md, introduction/quickstart.md, and the index at
  <https://docs.typesafe.ai/llms.txt> (all under <https://docs.typesafe.ai/>). [P]
- TypeSafe: <https://typesafe.ai>, <https://typesafe.ai/blog/introducing-system-one-models-and-jev>,
  <https://typesafe.ai/legal/privacy-policy>, <https://typesafe.ai/legal/data-processing>. [P]
- <https://simonwillison.net/2026/Sep/21/jev/> [S]; <https://www.langchain.com/blog/building-a-harness-with-jev> [S];
  <https://www.together.ai/blog/how-to-train-your-own-jev> [S/third-party];
  <https://opper.ai/blog/jev-vs-kev-open-decision-model> [S]; <https://github.com/arjun988/Kev> [S];
  <https://developers.cloudflare.com/ai/models/typesafe/jev/> [S].
- Unverified aggregator claims used only where marked: waitlist removal on 2026-09-21 and a $5 credit.
