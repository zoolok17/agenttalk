# agenttalk — Test Coverage Report

_Generated 2026-07-18. Suite state: **2,675 executed tests** across **62 `test_*.py` files** (~2,240 raw `def test_` functions; parametrization inflates to the executed count). Measured against master `684ff58`._

---

## 1. Executive summary

- **Volume & shape:** ~2,675 tests, **deterministic by policy** — there are **zero `xfail`/`flaky`/`reruns`/`retry` markers** in the entire suite; flakiness is engineered out (e.g. bounded connect-retry in the web client), not annotated.
- **Style:** predominantly **pure-Python unit / fixture tests** over a `tmp_path`-rooted `Store`, with targeted **real-subprocess integration** concentrated in: the `test_powershell_*` shim/host tranche, the live tranche of `test_supervisor.py`, `test_concurrency.py`, real-HTTP `test_web.py`, and git-backed `test_assurance`/`test_e2e_lifecycle`.
- **The single most important caveat:** **Python line-coverage structurally under-counts this project.** The supervisor's real logic lives in a **PowerShell template string executed in real `pwsh`** (`Win32_Process` snapshots, `File.Replace` semantics, sharing-violation handling). `coverage.py` cannot see PowerShell execution, so `supervisor.py` will always read low even though its behavior is exercised by real-pwsh integration tests. **The right metric for the supervisor is scenario/behavioral coverage (Task #24), not line %.**
- **Cross-platform posture (requirement: Windows + macOS + Linux):** the **Python core** (bus/store/CLI/wrapper) is **already cross-platform** and CI-tested on **Windows/macOS/Ubuntu × Python 3.10–3.13** (see `docs/ASSURANCE.md`). The **supervisor is the platform gap**: it currently requires **PowerShell Core 7+** and uses Windows-only `Win32_Process`; a **POSIX bash supervisor is a stated-but-unbuilt follow-up** (`cli.py:10485`). So "runs on all three" is true for the core today, and the open work is a **portable (or per-platform) supervisor** + the crash-harness (#24) exercising it on each OS. This report's earlier drafts over-emphasized Windows; the corrected posture is *cross-platform core, supervisor portability in progress.*
- **Top risk areas** (detail in §5): (1) supervisor crash / multi-instance race matrix, (2) real-pwsh coverage is Windows-runner-dependent → **false-green on POSIX CI**, (3) web/dashboard depth, (4) `cli.py` breadth-over-depth.

---

## 2. Coverage numbers (core Python modules)

> Measured with `coverage.py 7.15.2` / `pytest-cov` over the **pure-Python core test set** (store, threads, close, intents, knowledge, dead-letter, lanes, validation, signing, attention, capacity, gates, assurance, wrapper-loop, watchdog, doctor, coordination, concurrency, teams). The real-pwsh/dashboard suites are **excluded** here because their coverage isn't Python-line-measurable (see §1 caveat) — they're covered behaviorally.

**Core-set total: 63%** (27,871 stmts, 10,382 missed; 1,319 tests, 6m36s). This **understates real coverage** — modules whose suites were excluded from this run (`web.py`, `supervisor.py`, most of `cli.py`, `deadman.py`, `ephemeral.py`, `onboarding.py`, `powershell_host.py`, `supervisor_lifecycle.py`) read artificially low here. Full-suite (Windows-inclusive) numbers would be materially higher for those.

**Well-covered core (this run):**
| Module | Cover | | Module | Cover |
|---|---|---|---|---|
| `threads.py` | **95%** | | `attention.py` | 91% |
| `knowledge.py` | **92%** | | `capacity.py` | 91% |
| `health.py` | 91% | | `lanes.py` | 89% |
| `coordination_stall.py` | 89% | | `close.py` | 87% |
| `gates.py` | 87% | | `skill_currency.py` | 86% |
| `signing.py` | 84% | | `lesson_context.py` | 82% |
| `assurance.py` | 79% | | `doctor.py` | 78% |
| `store.py` | 75% | | `intents.py` | 73% |

**Under-counted here (their suites excluded — NOT true gaps):** `web.py` 15%, `deadman.py` 15%, `supervisor_lifecycle.py` 17%, `transcript.py` 20%, `ephemeral.py`/`onboarding.py` 21%, `powershell_host.py` 22%, `supervisor.py` 40% (see §1 caveat — its PowerShell template is *never* Python-line-measurable), `codex_config.py` 40%, `cli.py` 62% (most CLI tests excluded).

**How to reproduce / regenerate:**
```
set PYTHONPATH=<repo>\src
py -3.10 -m pytest tests/ --cov=agenttalk --cov-report=term-missing --cov-report=html:htmlcov -p no:cacheprovider
```
(Full-suite line coverage requires a **Windows runner** to exercise the real-pwsh tranche; on POSIX those tests skip and the pwsh-adjacent modules under-report.)

---

## 3. Test catalog — by functional area (what & how)

### Messaging / bus core (send · recv · threads · closure)
`test_store.py`(110) · `test_threads.py`(67) · `test_recv_api.py`(10) · `test_reply_tail.py`(19) · `test_validation.py`(21) · `test_intents.py`(46) · `test_release.py`(19) · `test_reset.py`(17) · `test_domains.py`(9)
- **What:** the file-bus contract from primitives up — ID monotonicity under contention (`test_new_id_is_strictly_monotonic_under_load`), roster-gated send (`_rejects_unknown_sender/_recipient/_self_mail`), per-agent cursor vs scoped-thread cursor (`_scoped_commit_marks_thread_seen_only_not_global_cursor`), obligation ("ball") ownership across typed responses (`_needs_info_flips_obligation_back_to_requester`), closure only on recognized terminal statuses, failsafe reads that drop forged records but still surface them.
- **How:** pure-Python unit / fixture-driven on `tmp_path` `Store`. No subprocess.
- **Thin:** body/meta size + unicode edge cases spot-checked only; "under load" is a bounded loop, not stress.

### Store / persistence / signing / atomicity
`test_store.py` · `test_signing.py`(46) · `test_atomic.py`(12) · `test_compact.py`(16)
- **What:** HMAC-v1 signing round-trips + every rejection path (tampered/wrong-key/unsupported-version); the **Windows atomic-write path** (`os.replace` retry, sandbox WinError5 latch, direct-write fallback — `_latch_stays_false_on_transient_winerror5_then_retry_success`); compaction that never archives invalid/pinned files and is crash-resumable.
- **How:** pure-Python, heavy `monkeypatch` of `os.replace` to inject IO faults; `test_atomic` is `skipif(os.name != "nt")`.
- **Thin:** signing key-rotation / multi-key-id coexistence.

### Supervisor & liveness / restart  ← **highest-risk area**
`test_supervisor.py`(236, largest) · `test_supervisor_lifecycle.py`(20) · `test_deadman.py`(12)
- **What:** liveness classification matrix (heartbeat-staleness + PID/start-time: `_alive_stale_without_hook_is_suspect_warn_not_kill`, `_pid_reuse_start_mismatch_is_not_alive`); claim/marker lock ordering + fail-closed refusals (`_claim_rechecks_selection_before_marker_write`, `_refuses_reused_pid_locator_without_marker`); generated `supervisor.ps1` is BOM-free ASCII, parses under real pwsh, and invokes exactly the CLI args Python implements; deadman fails closed on every corruption class.
- **How:** **mixed** — most of the 236 are pure-Python planner tests over synthetic observation dicts; a ~57-site real-subprocess tranche drives the live generated `supervisor.ps1` via `_start_live_generated_supervisor`, gated by `_pick_powershell()` (returns `None` off-Windows).
- **Thin / KNOWN GAP:** no test runs **two real supervisors racing one marker**; live-supervisor tests are single-instance and short-lived. → **Task #24**.

### Wrapper turn-loop & watchdog & health
`test_wrapper_loop.py`(105) · `test_wrapper.py`(24) · `test_turn_watchdog.py`(36) · `test_work_heartbeat.py`(24) · `test_wrapper_health.py`(10)
- **What:** prompt assembly (never emits bare bus commands); session resume ledger across codex/claude degrading on torn reads; liveness-stamping/degradation classifier per adapter; turn watchdog kills only aged non-codex descendants, fails open on missing snapshot; health file is failsafe and never leaks message content.
- **How:** pure-Python with adapter fixtures feeding **hand-authored** codex/claude event samples; thread-driven heartbeat ticker.
- **Thin:** adapter tests rely on synthetic samples — **real CLI output-format drift wouldn't be caught**; watchdog runs against synthetic process trees.

### PowerShell host & artifacts  ← **Windows-runner-dependent**
`test_powershell_host.py`(22) · `test_powershell_functional.py`(6) · `test_powershell_artifacts.py`(11) · `test_powershell_cli.py`(6) · `test_powershell_doctor.py`(5)
- **What:** real pwsh executes generated shims end-to-end (shim→cmd.exe→Python claim chain); WinPS 5.1 rejected pre-sentinel; artifact generation deterministic + keyed to python-pin + checkout identity, validation catches drift; probe timeout reaps descendants.
- **How:** the **most real-subprocess-heavy area** — `WINDOWS_ONLY = skipif(sys.platform != "win32")` + per-test `which("pwsh")` skips; subprocesses carry `PYTHONPATH=<src>` + `AGENTTALK_PYTHON` so the checkout runs (not the stale global install).
- **Thin / FALSE-GREEN RISK:** **entirely skipped on POSIX runners** — a POSIX-only CI reports green while exercising none of the generated-PowerShell runtime.

### CLI surface
`test_cli.py`(203) · `test_attention_cli.py`(28) · `test_lead_chat.py`(19) · `test_relay_wp4.py`(18) · `test_codex_config.py`(17) · `test_install_skills.py`(18)
- **What:** self/peer resolution + exit-2 discipline (`_send_exits_2_when_peer_ambiguous`), bootstrap dry-run, fanout UX guidance, idempotent + encoding-preserving config mutations.
- **How:** pure-Python via `_run([...])` in-process + `capsys`; exit codes asserted directly.
- **Thin:** **breadth-over-depth** — `cli.py` is 12.9k LOC; happy-path + arg errors dominate; mid-command IO-failure / partial-write recovery inside handlers is light.

### Web / dashboard  ← **thin vs risk**
`test_web.py`(157)
- **What:** disconnect handling swallows only genuine client disconnects (type+errno scoped, never real errors); HTTP status contract (405/404, GET-only), path-traversal rejection; projections fail closed on corrupt roster.
- **How:** **real HTTP integration** — `serve_in_thread(port=0)` in a daemon thread, hit over `http.client`, bounded connect-retry to absorb bind races.
- **Thin:** the write/action path (`enable_actions=True`), concurrent-request load, and **byte-level served-asset/CSP integrity** (the invisible-bytes class) are lightly exercised; frontend JS is only smoke-checked (not pytest).

### Lanes / ownership / lead-loop
`test_lanes.py`(117) · `test_lead_loop*.py`(39/16/17/29) · `test_ephemeral_reviewers.py`(19)
- **What:** segment-aware lane bounds (not naive string-prefix), overlap/subset holds, forged/stale-epoch approver rejection; lead-loop lease state machine — single owner under concurrency, steal only from expired/dead owners, manual identity never auto-stolen; exclusive ephemeral-reviewer launch that cleans partial state.
- **How:** pure-Python + **real threads** for concurrency; partial-write parametrization (`write/fsync/close`).

### Knowledge layer
`test_knowledge.py`(84)
- **What:** publish-is-uncurated + latest-by-key projection skipping invalid; causal-curation integrity (must bind to real parent, can't mutate payload/author, can't reopen tombstone from stale parent); legacy back-compat readable.
- **How:** pure-Python event-log unit tests.

### Dead-letter / poison
`test_dead_letter.py`(61) · `test_dead_letter_resolve.py`(15)
- **What:** v0.30.0 head-of-line regression fixed; dead-letter unblocks queue + counts as progress; classification (transient-below-cap retries, global-infra never auto-DL, ambiguous DLs at ceiling, torn counter degrades safe); resolve/requeue authority-gated + force-guarded + traversal-safe + survives reset.
- **How:** pure-Python, numbered-scenario style; "durability across relaunch" is in-process simulation, not a real restart.

### Coordination & stall
`test_coordination.py`(52) · `test_coordination_stall.py`(26)
- **What:** scoped-wait scan bounds + "composing" extension without double-delivery; loud coordination-stall warning fires once, requires two matching polls, ignores future-dated heartbeats (v0.77.0 fixes); await path-safety; fail-quiet-with-diagnostic on generation mismatch.
- **How:** pure-Python over synthetic heartbeat/poll timelines.

### Assurance / gates / close
`test_assurance.py`(33) · `test_gates.py`(24) · `test_close.py`(50) · `test_close_signoffs.py`(38) · `test_e2e_lifecycle.py`(2)
- **What:** manifest fails closed on unknown keys; gate semantics (skipped-blocker HOLD, scope satisfaction, corrupt-state refusal leaves file intact); close HOLD/GO + lens-ack authorization/staleness; sign-off requires distinct live candidates, stale/NA acks don't count.
- **How:** mostly pure-Python; `test_assurance`/`test_e2e_lifecycle` `skipif(which("git") is None)` and shell out to real git; only **2 true e2e lifecycle tests**.

### Capacity / attention / teams / roster · Doctor / skill hygiene · Runtime ergonomics / concurrency
`test_capacity.py`(39) · `test_attention.py`(36) · `test_teams.py`(49) · `test_doctor.py`(65) · `test_skill_currency.py`(35) · `test_runtime_ergonomics.py`(37) · `test_concurrency.py`(27)
- **What:** capacity snapshots parse Claude-statusline/codex-rollout (skip untrustworthy timestamps); attention ranking determinism + failsafe reader + dismiss authority; roster/group validation + audience resolution; doctor check categories + HMAC key-location warning + error rollup; skill-currency linter (scans only code spans); effort/model token precedence (tail>flag>config); **the config-lock protocol under real thread/process contention + crash-recovery + symlink-attack refusal**.
- **How:** pure-Python; `test_concurrency` spawns **real subprocesses** + threads (the most rigorously-raced primitive); capacity parsing uses hand-authored CLI samples (drift risk).

---

## 4. Testing patterns & infrastructure

- **conftest fixtures:** `store_root` / `store` (tmp_path-rooted), and an **autouse `_clear_agenttalk_env`** stripping all `AGENTTALK_*` vars each test → deterministic resolution regardless of host shell (guards the stale-install hazard).
- **Isolation:** every test under `tmp_path`; real-subprocess tests set `PYTHONPATH=<repo>/src` + `AGENTTALK_PYTHON=sys.executable` so spawned shims run the **checkout under test**, not the operator's stale global install.
- **Shared helpers:** `_team()` (supervisor), `_run()` (in-process CLI + capsys), `_serve`/`_urlopen` (real web server), and the live-supervisor trio `_start_live_generated_supervisor`/`_wait_for_live_supervisor`/`_stop_live_supervisor` fed by `_live_supervisor_config`.
- **Real-pwsh gating (two mechanisms):** `_pick_powershell()` returns `None` unless `os.name == "nt"` (deliberately stricter than `which("pwsh")` because POSIX runners ship pwsh but can't run the `.cmd` shim); plus `WINDOWS_ONLY` skipif + per-test `which("pwsh")` skips. **Consequence:** the real-pwsh tranche is inert on POSIX CI.

---

## 5. Coverage gaps / risk areas (ranked)

1. **Supervisor liveness / crash / multi-instance race matrix** — *(Task #24, in progress)*. Unit-level claim refusals are thorough, but no test runs two real supervisors racing one marker; live tests are single-instance/short-lived. This session's 3 supervisor bugs were caught by review/prod, not tests.
2. **Real-pwsh coverage is Windows-runner-dependent — false-green risk.** A POSIX-only CI run reports green while exercising none of the generated-PowerShell runtime. **Verify a Windows runner is in the CI matrix and that the pwsh tranche actually executes there** (not silently skipped).
3. **Web / dashboard depth** — thin on the write/action path (`enable_actions`), concurrent load, and byte-level served-asset/CSP integrity (the invisible-bytes class).
4. **`cli.py` breadth-over-depth** — 12.9k LOC; mid-command IO-failure / partial-write recovery inside handlers is light.
5. **System-wide concurrency beyond the config-lock** — the lock is exhaustively raced; supervisor+wrapper+CLI simultaneous operation on one store is not.
6. **Adapter/capacity parsing vs real CLI output** — validated against hand-authored codex/claude samples; real output-format drift passes tests but breaks in the field.
7. **End-to-end lifecycle** — only 2 true e2e tests; most "durability across relaunch/reset" claims are in-process simulations, not real restarts.

---

## 6. Tooling recommendations

**Highest leverage — targets the exact gap this session exposed (bugs slipping past tests):**
- **Hypothesis (property-based).** Best fit. The bus protocol, resolver state machine, and enforcement invariants ("never double-commit", "no fail-open", "no double-launch") are *properties*. Hypothesis generates random event/message/crash sequences and asserts the invariant — finding the edge cases we hand-prove in review. Start with the resolver + dead-letter classification + lane bounds.
- **Mutation testing (`mutmut` or `cosmic-ray`).** Measures whether tests actually *catch* bugs (breaks the code, checks a test goes red). Run on `supervisor.py` / `store.py` / the enforcement modules — it would have flagged the thin spots before prod. Directly answers "are our tests any good?"

**Mechanical wins:**
- **`coverage.py` / `pytest-cov`** (installed) — the numbers + `--cov-report=html` for line-level drill-down.
- **`pytest-xdist`** — parallelize the 2,675-test suite (~13 min serial → a few min).
- **`pytest-randomly`** — catches order-dependence / test-pollution (matters with a shared file-bus).
- **`tox` / `nox`** — automate the 3.10+3.14 matrix we run by hand + pin a clean env (guards the stale-install hazard).

**Docker / containers — fit under the cross-platform mandate (Windows/macOS/Linux):**
- **Core (bus/store/CLI/wrapper):** already cross-platform and CI-tested on all three OSes. A **Linux CI container** is a strong fit here — fast, reproducible, catches packaging/import issues (like the stale-install shadowing we hit). This is the bulk of the codebase.
- **Supervisor:** today it's PowerShell-Core + Windows-only `Win32_Process`; the **POSIX bash supervisor is an unbuilt follow-up** (see the cross-platform note in §1). Testing strategy by platform: **Windows** → real-pwsh harness on a Windows runner; **Linux** → once the POSIX supervisor exists, it's **fully containerizable** (the fake-agent crash matrix #24 runs great in a Linux container); **macOS** → needs a **real macOS runner** (pwsh-Core runs there, but process-snapshot behavior differs; containers don't help macOS). So Docker covers Linux well, but **cannot substitute for the Windows and macOS runners** — all three OSes need a real (or containerized-Linux) supervisor path exercised.
- **The "different scenarios" container that pays off now:** a **fake-model-gateway container** for the Qwen/OVH path — serves canned model responses to simulate 429s, timeouts, malformed/slow replies, so enforcement + wrapper tests exercise backend-failure modes **without paid OVH calls**. Platform-agnostic and high-value ahead of the Qwen canary.
- **CI-honesty rule:** the matrix must run the platform-specific supervisor path on **each** target OS — a Linux-container-only matrix gives false-green on the Windows/macOS supervisor (§5.2). #24 must be authored to run on whichever supervisor implementation exists per platform.

**Scenario-based (the "different scenarios" idea, done right):**
- **The fake-agent crash matrix (Task #24)** — a configurable stub the real generated supervisor launches, failing every realistic way (crash-on-start / crash-after-N / hang / wedge / crash-loop / bad-heartbeat / pid-reuse / record-launch-fail), asserting the supervisor's restart/liveness/no-double-launch/survival invariants. This is the scenario harness — **native Windows/pwsh**, not Docker.

---

## 7. Recommended next actions (priority order)

1. **Land Task #24** (supervisor crash matrix) — closes the #1 risk; codex-2's next task.
2. **Verify the CI matrix has a Windows runner that actually runs the pwsh tranche** (not silently skipped) — closes the false-green risk cheaply.
3. **Add Hypothesis property tests** for the resolver state machine + dead-letter classification + lane bounds — highest correctness leverage.
4. **Run mutation testing** on `supervisor.py` + enforcement modules to find where tests pass on broken code.
5. **Adopt `pytest-xdist` + `tox`/`nox`** to make the full (Windows-inclusive) matrix fast and reproducible in CI.
6. **Deepen web/dashboard** (action path + byte-level asset/CSP integrity) and add real-restart durability tests (dead-letter/reset).
7. Consider a **fake-model-gateway container** ahead of the Qwen enforcement canary, to test backend-failure modes without paid calls.
