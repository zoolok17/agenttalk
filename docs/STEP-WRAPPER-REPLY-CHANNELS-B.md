# Increment B — shell-correct CLI form + explicit per-kind reply channel

Branch `fix/wrapper-reply-channels`, on top of increment A (`6697cc7`). Fixes field fact 2: every
"HOW TO REPLY" invocation in `prompt.py` was hardcoded to PowerShell's `& "$env:AGENTTALK_PY" ...`
form for every wrapped seat regardless of its actual shell. A claude seat's Bash tool runs
git-bash; the qwen developer seat nested `powershell -Command` inside `bash eval`, hit two parse
errors ("unexpected EOF while looking for matching quote"), and its third attempt returned exit 0
with empty output and no message written — the reply was lost with no visible failure.

## The fix

1. **Shell-correct rendering** (`prompt.py`). Every invocation in `_DEFAULT_RULES`,
   `_BUS_COMMAND_CONTRACT`, `_CADENCE_RULES`, and the per-kind "HOW TO REPLY" section is written
   ONCE, in PowerShell form — confirmed by grep to be a single, exact, literal substring
   (`& "$env:AGENTTALK_PY" -m agenttalk`) with zero quoting variants across all 20 occurrences.
   Rather than duplicate every rules string in two shells, `_render_for_shell` rewrites the WHOLE
   assembled prompt as the last step before it returns: `reply_shell == "bash"` replaces every
   occurrence with `"$AGENTTALK_PY" -m agenttalk` (drops PowerShell's `& ` call operator, entirely
   meaningless — a syntax error, in fact — in bash; drops `$env:` for bash's own plain `$` env-var
   syntax). `assemble_turn_prompt` and `assemble_cadence_prompt` both gained a
   `reply_shell: str = "powershell"` parameter (default preserves today's exact output byte-for-byte
   for every existing caller; named `reply_shell` rather than the shorter `shell` from the start of
   this record's own history — see the dev-gate note below for why).
2. **Per-kind explicit reply-channel text** (`prompt.py`, same function). Previously every kind
   sharing the draft channel (question/message/wake, and now task per increment A) got one generic
   paragraph. A task inbound now gets its own "HOW TO REPLY TO THIS MESSAGE" text: the CLI form
   explicitly shows `--kind task-response`, and a `--na`/bare-refusal is explicitly ruled out in
   favor of `--meta status=declined --meta reason=<why>` (matching the exact restriction
   `cmd_reply` already enforces server-side — see increment A's own enumeration of
   `gates.py`/`cli.py`'s task-response handling). The draft-channel paragraph, when shown for a
   task inbound, now also states it "publishes as a typed task-response" so a seat reading only
   that section (not the CLI form below it) still knows what kind lands.
3. **Shell resolution** (`supervisor.py`, new `resolve_reply_shell(config, cfg_agent, *, cli)`).
   Per-agent `reply_shell` in `supervisor.json` wins over a global `reply_shell` setting, which
   wins over a per-CLI default (`claude`/`qwen` -> `bash`, `codex` -> `powershell`, anything else
   -> `powershell` — a safe default: it was the ONLY behavior every wrapped seat got before this
   increment). Mirrors `resolve_window_style`'s own per-agent -> global -> default chain; an
   invalid value at either level is never coerced into a false positive — it is ignored, falling
   through to the next source, all the way to the CLI default, rather than rendering an invocation
   the child's real shell cannot parse.
4. **Wiring** (`cli.py`, `wrapper/run.py`). `_wrap_loop_mode`'s own caller (the `agenttalk wrap
   --loop` command handler, where `sup_cfg`/`cfg_agent`/`args.cli` are already in scope for the
   existing dead-letter-cap/watchdog/work-heartbeat resolvers) now also resolves
   `reply_shell = _sup.resolve_reply_shell(sup_cfg, cfg_agent, cli=args.cli)` and threads it
   through as a new keyword-only, defaulted parameter: `_wrap_loop_mode` -> `make_drive` /
   `make_cadence_drive` -> `assemble_turn_prompt` / `assemble_cadence_prompt`. Every new parameter
   in this chain defaults to `"powershell"` and sits after the existing `*` keyword-only marker in
   every signature it touches — purely additive, no existing positional or keyword call site
   anywhere in the codebase (11 test files alone call `make_drive`/`make_cadence_drive` directly)
   can be affected by its mere presence.

## Where this does NOT reach

`wrapper_run.run_wrapper` (the separate, older "wrap one CLI run" degraded-mode path, reached only
when `agenttalk wrap` is invoked WITHOUT `--loop`) never calls `assemble_turn_prompt` at all — it
has no per-turn prompt to make shell-aware, so `reply_shell` was not threaded there. Confirmed by
reading its own body: it drives adapter-level structured-stream parsing, not a classify-and-reply
turn.

## Tests

- `test_supervisor.py`: 3 new (`resolve_reply_shell` default-by-CLI; per-agent/global precedence;
  an invalid value at one level falls through rather than sticking).
- `test_wrapper_loop.py`: 3 new (`assemble_turn_prompt`'s default stays byte-identical to the
  PowerShell-only behavior every existing caller relies on; `reply_shell="bash"` rewrites every
  invocation, confirmed by both presence of the bash form and absence of `$env:AGENTTALK_PY`/
  `& "` anywhere in the output; the task-kind prompt states `--kind task-response` explicitly and
  a non-task kind picks up none of that text) + 1 new (`assemble_cadence_prompt` respects
  `reply_shell` too, guarding against the cadence path silently staying PowerShell-only while the
  ordinary turn path got fixed).
- Full run: `test_reply_draft_delivery.py` 39/39 (unaffected — draft-channel mechanics untouched by
  B), `test_wrapper_loop.py -k "prompt or cadence"` 15/15, `test_supervisor.py -k "reply_shell or
  window_style or resolve_stuck_after or resolve_dead_letter"` 7/7, `test_stub_agent_canary.py`
  (spot-check on an unrelated `make_drive` caller, confirming the new keyword-only defaulted param
  is a true no-op for existing callers) 7/7. `ruff check` and `py_compile` clean on every touched
  file (`prompt.py`, `run.py`, `supervisor.py`, `cli.py`).

## Dev-gate follow-up: `shell` renamed to `reply_shell` everywhere

PR #189's ruff and bandit lanes went red on every lane after the pytest legs turned green: ruff
`S604` and bandit `B604` both flag ANY call with a truthy keyword argument literally named `shell`
as a subprocess `shell=True` risk — by NAME alone, regardless of what the called function actually
does. `assemble_turn_prompt`/`assemble_cadence_prompt` (and the private `_render_for_shell` helper
they call) originally took a parameter named `shell`; `wrapper/run.py`'s own two call sites and two
`test_wrapper_loop.py` test call sites all invoked them with `shell=` as an explicit keyword,
tripping both scanners even though this module spawns no subprocess at all — a real-looking finding
by design (neither scanner reads the callee's own body, only the call-site syntax), so the fix is a
rename, never a `nosec`/`noqa` suppression (a suppression on a real-looking finding is exactly what
the tripwire must keep catching).

Renamed `shell` -> `reply_shell` in every signature and every call site that used it as a keyword:
`prompt.py`'s `assemble_turn_prompt`, `assemble_cadence_prompt`, and `_render_for_shell` (the last
one is called positionally, so it was never itself flagged, but renamed anyway for consistency
since its own body still reads the parameter by name); `run.py`'s two `_prompt.assemble_*` call
sites; the two `test_wrapper_loop.py` fixtures that called either function with `shell=`. Nothing
in `loop.py` or `supervisor.py` ever used a `shell=` keyword call (confirmed by a repo-wide grep) —
both already used `reply_shell` throughout, from this increment's own original naming at that
layer. `resolve_reply_shell` itself (`supervisor.py`) was never affected; its own parameters are
`config`/`cfg_agent`/`cli`, never `shell`.

`ruff check src tests`: all checks passed. `bandit -q -r src`: 0 issues (Undefined/Low/Medium/High
all 0). Full run: `test_wrapper_loop.py -k "prompt or cadence"` 15/15, `test_supervisor.py -k
reply_shell` 3/3, `test_reply_draft_delivery.py` + `test_threads.py` 117/117 (unaffected, spot-
checked as a broader regression net since both files import `prompt`/`run` transitively).

## Confidentiality sweep

`grep -riE '<protected-1>|<protected-2>'` (the two protected strings of the local confidentiality
rule) over this file and the increment's own diff — 0 hits. Public GitHub repo; no client name,
credential, or hostname belongs in any tracked content regardless.
