// Real-browser check for the console v2 stream, driven over the DevTools protocol.
// Usage: node console2_browser_check.mjs <browser-exe> <page-url> <profile-dir> [launch-flags-json] [sabotage]
// launch-flags-json is a JSON array of the platform-specific launch flags (see
// test_console2_browser.py's _browser_launch_flags, the pure function that picks them - the
// sole caller always supplies it); an empty/omitted array means no headless/GPU/sandbox flags
// at all, useful only for manual debugging with a real, visible browser window.
// sabotage, when the literal string "scroll", installs a MutationObserver that snaps the thread
// back to its bottom on every age redraw - a NEGATIVE CONTROL for the F1 scroll check (dev-4's
// own finding): tests/test_console2_browser.py's own negative-control test asserts this makes
// afterRedraw.top come back wrong, proving the atomic setup+baseline fix below actually waits for
// a redraw AFTER scrollTop is set, not one that already happened before it.
// Prints one JSON object of measurements; tests/test_console2_browser.py asserts on it.
//
// What the DOM stub cannot show and this does: a detached element has no scroll layout (the thread
// must be scrolled after insertion), and a redraw that replaces a focused control drops focus to <body>.
import { spawn } from 'node:child_process';
import { pathToFileURL } from 'node:url';

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// PR #230 delta review, F2: a single per-HTTP-attempt cap. Every attempt gets the SMALLER of this
// and whatever remains of the shared budget, so one stalled attempt cannot eat the whole thing,
// and several retries stay possible within the overall deadline.
export const PER_REQUEST_CAP_MS = 10000;
// The overall startup budget (DevTools up AND a page target found) - ONE shared elapsed-time
// clock, not two independent ones (see makeDeadline/F2 below).
export const STARTUP_BUDGET_MS = 90000;

// A single overall elapsed-time budget, meant to be created ONCE and threaded through every wait
// composed from it. PR #230 delta review, F2: the previous code gave waitForPageTarget its OWN
// fresh 90s timeout, which internally called json() - ALSO its own fresh 90s timeout - so a
// reviewer's fake-clock composition measured 180,050 ms total: two independent deadlines, each
// blind to how much the OTHER had already spent. A shared `deadline` object fixes that at the
// root - every consumer asks the SAME clock "how much is left", never starts its own.
export function makeDeadline(totalMs, { now = Date.now } = {}) {
  const start = now();
  return {
    remainingMs: () => Math.max(0, totalMs - (now() - start)),
    expired: () => now() - start >= totalMs,
  };
}

// PR #230 delta review, F3: thrown ONLY when `checkExited` reports a real process exit -
// deliberately a DISTINCT class from an ordinary "not ready yet" failure (connection refused, a
// malformed response), so a blanket `catch` elsewhere can tell "the browser is DEAD, stop retrying"
// apart from "try again shortly" and never swallow the former. The previous code's outer
// waitForPageTarget loop caught its inner json() call's own already-correct fatal-exit error with
// the SAME catch-all used for transient failures, discarding the exit code/stderr and just
// retrying blindly until its own timeout - this is what let that information get lost.
export class FatalExitError extends Error {
  constructor(describe, exitInfo) {
    super(`${describe || 'condition'}: the process exited before it was met (${JSON.stringify(exitInfo)})`);
    this.name = 'FatalExitError';
    this.exitInfo = exitInfo;
  }
}

// Retries `fn` until it resolves to a value other than `undefined`, bounded by a SHARED `deadline`
// (a makeDeadline() - never its own fresh timeout, see above).
//
// PR #230 delta review, F2 ("a never-settling operation escapes the deadline"): each iteration
// races the current `fn()` ATTEMPT against ONE poll interval (never the full remaining budget in
// one race - an earlier version of this fix tried that and, under a fake clock whose `sleep`
// necessarily mutates shared state as soon as it is CALLED, discovered `Promise.race` does not
// cancel the loser: BOTH racers still run to completion regardless of which "wins", so a
// remaining-time sleep silently jumped the fake clock to the deadline on every single iteration,
// corrupting the very thing under test). A single still-pending `fn()` attempt is preserved
// (`pending`) and re-raced on each subsequent iteration - never re-invoked - so a slow-but-alive
// operation is still waited on correctly, while the LOOP itself keeps re-checking checkExited and
// the deadline every `intervalMs`, and so cannot hang past the budget even if `fn()` itself never
// settles. This is a backstop: the real fix for a hung network call is giving THAT call its own
// AbortSignal (see fetchJsonOnce below), which actually releases the underlying resource.
//
// A FatalExitError is NEVER swallowed as "not ready yet" (F3): it propagates immediately, with its
// exit code/stderr intact, from anywhere inside `fn`. Every dependency is injectable (`sleepFn`)
// so this is unit-testable against a stub with no real browser, process or timers - see
// console2_browser_check_wait.test.mjs.
export async function waitForDeadline(fn, {
  deadline, intervalMs = 150, sleep: sleepFn = sleep, checkExited, describe, getDiagnostics,
} = {}) {
  let pending = null;   // the in-flight fn() attempt, if any is still outstanding from a prior tick
  for (;;) {
    const exitedBefore = checkExited && checkExited();
    if (exitedBefore) throw new FatalExitError(describe, exitedBefore);
    if (deadline.expired()) {
      // Connector P2 (N1): the browser can stay ALIVE (no exit, so no FatalExitError) while never
      // exposing the DevTools endpoint - exactly the slow-startup path this whole change exists to
      // diagnose. The old fixed-attempt-budget error always included the accumulated stderr; this
      // one must too, not only the (already-covered) fatal-exit path.
      const extra = getDiagnostics && getDiagnostics();
      throw new Error(`${describe || 'condition'} did not happen within the shared budget`
        + (extra ? `; ${extra}` : ''));
    }
    if (pending === null) {
      pending = (async () => {
        try { return { ok: true, value: await fn() }; } catch (e) { return { ok: false, error: e }; }
      })();
    }
    const tickMs = Math.max(0, Math.min(intervalMs, deadline.remainingMs()));
    const outcome = await Promise.race([
      pending.then((settled) => ({ ticked: false, settled })),
      sleepFn(tickMs).then(() => ({ ticked: true })),
    ]);
    if (outcome.ticked) continue;   // fn() is still outstanding; re-race the SAME attempt next tick
    pending = null;                 // this attempt is done - a fresh one starts next iteration if needed
    if (!outcome.settled.ok) {
      if (outcome.settled.error instanceof FatalExitError) throw outcome.settled.error;
      continue;   // an ordinary failure - not ready yet, retry with a fresh attempt
    }
    if (outcome.settled.value !== undefined) return outcome.settled.value;
  }
}

// Issue #229: `/json/list` can answer OK before the browser has created its initial page target
// yet (a startup race, distinct from the DevTools HTTP endpoint merely being up at all) - the
// previous code took the FIRST list response unconditionally and crashed on the missing target
// (`Cannot read properties of undefined (reading 'id')`) rather than waiting for one to exist.
// `listTargetsOnce` must be a SINGLE bounded attempt (never its own retry loop, or F2's nested-
// deadline bug returns) - opts.deadline/opts.checkExited are required, shared with whatever else
// draws from the same overall budget.
export async function waitForPageTarget(listTargetsOnce, opts) {
  return waitForDeadline(async () => {
    const targets = await listTargetsOnce();
    return Array.isArray(targets) ? targets.find((t) => t.type === 'page') : undefined;
  }, { intervalMs: 150, describe: 'a page target appearing in /json/list', ...opts });
}

// Everything below this line is the check's own entrypoint - spawning a real browser and driving
// it over CDP - guarded so importing the pure helpers above (for a unit test) never also does that.
async function main() {
const [exe, pageUrl, profile, flagsJson, sabotage] = process.argv.slice(2);
const extraFlags = flagsJson ? JSON.parse(flagsJson) : [];

// The page can carry more than one `.c2-age` label at once (one per open card), each on its OWN
// independent clock, and "the first .c2-age" is neither stable (DOM/queue order depends on
// relative age, which the fixture or a real store can change run to run) nor guaranteed to ever
// tick within any bounded wait (an already hour-scale label can go a full simulated hour, sped up
// or not, without its rendered text changing - exactly what stalled a real CI runner). Watch a
// SPECIFIC card instead, selected by its own stable key - card-2, the one
// tests/test_console2_browser.py's fixture keeps as the OLDEST of its two cards (so it is the one
// still open once this check's own R2/R3 key-path steps below defer the other away) and near-zero
// aged (so it stays well clear of hour granularity for the life of one run). Matched by
// data-c2-card's own "team|id" convention regardless of what "team" resolves to, since only the id
// half is ours to fix.
const WATCHED_AGE_SELECTOR = '[data-c2-card$="|card-2"] .c2-age';

const sabotageScrollScript = sabotage === 'scroll'
  ? `window.__sabotageObserver = new MutationObserver(() => { window.__thread.scrollTop = 999999; });
     window.__sabotageObserver.observe(document.querySelector('${WATCHED_AGE_SELECTOR}'), { childList: true, characterData: true, subtree: true });`
  : '';
const PORT = 9300 + Math.floor(Math.random() * 500);

// `profile` is a fresh, unique directory per test run - the caller's job
// (test_console2_browser.py's `page` fixture uses pytest's own tmp_path); a reused or
// already-open profile directory makes Chromium refuse to start outright.
const child = spawn(exe, [
  ...extraFlags, `--user-data-dir=${profile}`,
  `--remote-debugging-port=${PORT}`, '--window-size=1240,780', 'about:blank',
], { stdio: ['ignore', 'ignore', 'pipe'] });

let childStderr = '';
child.stderr.on('data', (chunk) => { childStderr += chunk.toString(); });
let childExit = null;
child.on('exit', (code, signal) => { childExit = { code, signal }; });
const checkChildExited = () => childExit && { ...childExit, stderr: childStderr };

// ONE bounded GET, returning parsed JSON or `undefined` if the endpoint is not ready yet (any
// connection failure, non-OK status, or the request itself timing out) - NEVER a retry loop of
// its own (PR #230 delta review, F2: that is exactly what nested two independent deadlines).
// Retrying is the CALLER's job, against ONE shared budget. `remainingMs` bounds this ONE attempt
// via AbortSignal, capped at PER_REQUEST_CAP_MS, so a single stalled connection can never eat the
// whole shared budget - the real fix for a never-settling fetch (waitForDeadline's own race is a
// backstop for a stub `fn` in general, not a substitute for actually releasing the socket).
async function fetchJsonOnce(path, remainingMs) {
  const capMs = Math.max(1, Math.min(remainingMs, PER_REQUEST_CAP_MS));
  const r = await fetch(`http://127.0.0.1:${PORT}${path}`, { signal: AbortSignal.timeout(capMs) });
  return r.ok ? await r.json() : undefined;
}

// A ONE-SHOT DevTools HTTP action (never a "wait for ready" read like fetchJsonOnce above): brings
// `id` to the front. The endpoint replies text/plain, not JSON - reading it as text and surfacing
// any real failure is the whole N4 fix (never silently retried, never swallowed). Bounded by its
// own short, fixed cap (PR #230 delta review, F2: "the fetch has no abort signal") - this call
// happens well after startup, so it does not draw on the shared startup budget.
async function activateTarget(id) {
  const r = await fetch(`http://127.0.0.1:${PORT}/json/activate/${id}`, { signal: AbortSignal.timeout(PER_REQUEST_CAP_MS) });
  const body = await r.text();
  if (!r.ok) throw new Error(`could not activate the browser target: http ${r.status} ${body}`);
}

let seq = 0;
const pending = new Map();
const problems = [];
let ws;

function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error('page exception: ' + JSON.stringify(r.exceptionDetails.exception || r.exceptionDetails.text));
  return r.result.value;
}

// F1/F2 need at least one real redraw to have happened (the fake server ages its cards on every
// read, so the page's own poll-and-redraw cycle is what changes the .c2-age label's text) before
// the after-redraw measurements below mean anything. A FIXED sleep before checking once is a race:
// a slower CI runner (macOS in particular) can miss that window entirely, which is a genuine,
// correctly-refused precondition failure, not flakiness to paper over with a longer constant.
// This polls for the actual observed change instead, bounded so a real stall still fails fast and
// with a clear reason - never silently drops through and lets a stale reading pass as if it moved.
// A headless target can drift document.hidden/visibilityState back to backgrounded after enough
// real time with no INPUT event reaching it (observed even with the anti-occlusion launch flags
// and an explicit one-time activation): the stream's OWN F1/F2 waits happen right after a long
// run of real pressKey() calls and so are never exposed to this for long, but a purely passive
// poll loop like this one - exactly what the board's redraw wait is - can sit idle long enough to
// trip it, silently starving F6's OWN correct "pause while hidden" of the visibility it needs.
// keepAliveTargetId (module-scoped, set once `page` is known) is re-activated every iteration -
// a single local DevTools HTTP GET, cheap even at up to ~120 calls over a full 30s timeout.
let keepAliveTargetId = null;

async function waitForTextChange(selector, previousText, { timeoutMs = 30000, intervalMs = 250 } = {}) {
  const start = Date.now();
  for (;;) {
    if (keepAliveTargetId !== null) await activateTarget(keepAliveTargetId).catch(() => {});
    const current = await evaluate(
      `(() => { const a = document.querySelector('${selector}'); return a ? a.textContent : null; })()`,
    );
    if (current !== previousText) return { changed: true, elapsedMs: Date.now() - start, text: current };
    const elapsedMs = Date.now() - start;
    if (elapsedMs >= timeoutMs) {
      const hidden = await evaluate('document.hidden');
      throw new Error(
        `no redraw observed in ${(elapsedMs / 1000).toFixed(1)}s `
        + `(the watched label text stayed "${previousText}", selector "${selector}", docHidden=${hidden})`);
    }
    await sleep(intervalMs);
  }
}

function waitForAgeChange(previousText, opts) { return waitForTextChange(WATCHED_AGE_SELECTOR, previousText, opts); }

// macOS CI follow-up (N4 side-effect): a watched age already at hour/day granularity when a
// redraw wait is about to START cannot be relied on to change again within waitForTextChange's own
// 30s budget (fmtAge only advances "Nh" once a further 60 simulated minutes have passed, which at
// this fixture's 60x acceleration is another 60 REAL seconds). Wasted real time before this point -
// N4's ~30s of redundant activation retries being the exact case that tripped this on CI - could
// push the fixture's accelerated age past that boundary before either redraw check even started.
// Assert the starting granularity up front and fail clearly, rather than let the wait time out
// with a confusing "stayed the same" message that does not explain WHY it could never have moved.
function assertFineGrainedAge(text, pattern, label) {
  const m = pattern.exec(text);
  if (!m) throw new Error(`${label}: could not find its own age token in "${text}"`);
  if (m[1] === 'h' || m[1] === 'd') {
    throw new Error(`${label} is already at hour/day granularity ("${m[0]}" in "${text}") before the `
      + 'redraw wait started - it cannot be relied on to change again within the wait budget');
  }
}

// R1/R2: a synthetic `document.dispatchEvent(new KeyboardEvent(...))` only ever fires JS listeners -
// it never triggers the browser's OWN default actions (Tab moving focus, Enter activating a focused
// button or navigating a focused link). Those defaults are exactly what a focus trap and a
// native-Enter carve-out have to survive, so this drives the real input pipeline over CDP instead.
const KEYS = {
  Tab: { code: 'Tab', keyCode: 9, key: 'Tab' },
  Enter: { code: 'Enter', keyCode: 13, key: 'Enter' },
  Escape: { code: 'Escape', keyCode: 27, key: 'Escape' },
  '?': { code: 'Slash', keyCode: 191, key: '?', shift: true },
  j: { code: 'KeyJ', keyCode: 74, key: 'j' },
  ' ': { code: 'Space', keyCode: 32, key: ' ' },
};

async function pressKey(name, opts = {}) {
  const k = KEYS[name];
  const modifiers = (opts.shift || k.shift) ? 8 : 0;
  const base = {
    modifiers, windowsVirtualKeyCode: k.keyCode, nativeVirtualKeyCode: k.keyCode, code: k.code, key: k.key,
    text: k.key === 'Enter' ? '\r' : undefined, unmodifiedText: k.key === 'Enter' ? '\r' : undefined,
  };
  await send('Input.dispatchKeyEvent', { type: 'keyDown', ...base });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', ...base });
}

try {
  // PR #230 delta review, F2: ONE shared budget for the WHOLE startup sequence (DevTools coming
  // up, then a page target appearing) - the previous code gave each phase its own independent 90s
  // timeout, which composed to a measured 180s worst case. Both phases below draw on this SAME
  // `startupDeadline`; whatever the first phase spent is no longer available to the second.
  const startupDeadline = makeDeadline(STARTUP_BUDGET_MS);
  const getDiagnostics = () => `stderr:\n${childStderr}`;
  await waitForDeadline(() => fetchJsonOnce('/json/version', startupDeadline.remainingMs()), {
    deadline: startupDeadline, intervalMs: 150,
    checkExited: checkChildExited, describe: 'the DevTools endpoint (/json/version)', getDiagnostics,
  });
  // Issue #229: `/json/list` can answer OK before any `type === 'page'` target exists yet - the
  // old code took whatever the FIRST list response held unconditionally and crashed reading
  // `.id` off `undefined`. Wait for one to actually appear instead, still on the SAME budget.
  const page = await waitForPageTarget(
    () => fetchJsonOnce('/json/list', startupDeadline.remainingMs()),
    { deadline: startupDeadline, checkExited: checkChildExited, getDiagnostics },
  );
  keepAliveTargetId = page.id;
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const p = pending.get(msg.id); pending.delete(msg.id);
      if (msg.error) p.reject(new Error(msg.error.message)); else p.resolve(msg.result);
    } else if (msg.method === 'Runtime.exceptionThrown') {
      problems.push('exception: ' + JSON.stringify(msg.params.exceptionDetails.text));
    } else if (msg.method === 'Log.entryAdded' && msg.params.entry.level === 'error') {
      problems.push('log: ' + msg.params.entry.text);
    }
  };
  await send('Runtime.enable');
  await send('Log.enable');
  await send('Page.enable');
  // B8/F6: a headless target starts backgrounded (document.hidden/visibilityState report hidden)
  // unless explicitly brought to the front - activating it here makes the page-visibility checks
  // below exercise the SAME "visible" branch a real, focused browser tab would.
  //
  // N4 (PR #221 delta): /json/activate/<id> is a ONE-SHOT action, not a "wait for the DevTools
  // endpoint to come up" read - it replies text/plain ("Target activated"), never JSON. The
  // original code reused `json()` (built for polling /json/version|/json/list, which genuinely
  // are not up yet right after spawning the process) for this too; every read attempt failed the
  // JSON parse, was swallowed as "not up yet", and retried 200 times at 150ms - about 30 wasted
  // seconds on every browser scenario - while also hiding a genuine activation failure behind the
  // same swallowed catch. A single plain GET, and an explicit post-navigate visibility check that
  // FAILS CLEARLY rather than silently proceeding, replaces it.
  await activateTarget(page.id);
  try { await send('Page.setWebLifecycleState', { state: 'active' }); } catch (e) { /* older targets lack this; activateTarget above is the primary mechanism */ }
  await send('Page.navigate', { url: pageUrl });
  const stillHidden = await evaluate('document.hidden');
  if (stillHidden) {
    throw new Error('target activation did not make the page visible (document.hidden is still true)');
  }
  await sleep(3500);                                   // first poll, feeds, first draw

  const out = {};
  const measure = () => evaluate(`(() => { const t = document.querySelector('.c2-thread');
    return t ? { top: t.scrollTop, height: t.scrollHeight, client: t.clientHeight, connected: t.isConnected } : null; })()`);

  // F1: first draw is scrolled to the end
  out.initial = await measure();

  // Assert the watched age label exists (and so is the intended one - see WATCHED_AGE_SELECTOR
  // above) BEFORE relying on it anywhere below; a wrong or missing selector must fail clearly
  // here, not surface later as a confusing "no redraw observed" timeout.
  const watchedLabelText = await evaluate(
    `(() => { const a = document.querySelector('${WATCHED_AGE_SELECTOR}'); return a ? a.textContent : null; })()`,
  );
  if (watchedLabelText === null) {
    throw new Error(`watched age label not found: no element matches "${WATCHED_AGE_SELECTOR}"`);
  }
  out.watchedLabelInitialText = watchedLabelText;

  // F2: the rail's avatar <img> is reconciled in place, not torn down, across an ordinary redraw
  // (scoped to #c2-rail: the lead block in the stream has its own, already-reconciled, avatar)
  await evaluate(`(() => { window.__avatar = document.querySelector('#c2-rail .c2-avatar-img'); return !!window.__avatar; })()`);

  // M4b/R2/R3: one key path, run FIRST while focus is still untouched (document.body, from page
  // load) - j selects the first card (the fixture's two escalation cards, oldest first: card-2 then
  // card-1), Enter focuses its first action (Later, since these fixture cards carry no options at all).
  const cardKey = (i) => evaluate(`document.querySelectorAll('.c2-card')[${i}].getAttribute('data-c2-card')`);
  await pressKey('j');
  out.firstCardSelectedAfterJ = await evaluate(
    `(() => { const c = document.querySelector('.c2-card'); return !!c && c.className.indexOf('is-selected') >= 0; })()`,
  );
  const cardAKey = await cardKey(0);
  await pressKey('Enter');
  out.focusAfterEnterOnSelected = await evaluate(`(() => { const a = document.activeElement;
    return { tag: a.tagName, cls: a.className, sameAsFirstCardLater: a === document.querySelector('.c2-card .c2-later') }; })()`);

  // R3: a second j must move focus WITH the selection, onto card B itself - never left behind on
  // card A's Later (which is exactly what let a later Enter act on the wrong, no-longer-highlighted
  // card).
  await pressKey('j');
  const cardBKey = await cardKey(1);
  out.focusAfterSecondJ = await evaluate(`(() => { const a = document.activeElement;
    return { tag: a.tagName, cardKey: a.getAttribute('data-c2-card') }; })()`);
  out.secondJMovedFocusToCardB = out.focusAfterSecondJ.tag === 'ARTICLE' && out.focusAfterSecondJ.cardKey === cardBKey;

  // R2/R3: Enter now opens B (the actually-highlighted card); a second native Enter, with focus
  // natively on B's Later, must defer B specifically - never A, and never be diverted back to
  // "open the selection" again.
  await pressKey('Enter');
  out.focusAfterEnterOnB = await evaluate(`(() => { const a = document.activeElement;
    return { tag: a.tagName, cls: a.className }; })()`);
  await pressKey('Enter');
  await sleep(200);
  out.cardsAfterSecondEnter = await evaluate(`document.querySelectorAll('.c2-card').length`);
  out.remainingCardIsA = (await cardKey(0)) === cardAKey;

  // R2: with a card selected, a native Enter on a focused, unrelated interactive control (a theme
  // button) must activate THAT control, never get diverted to the selection shortcut.
  await pressKey('j');   // select whatever card is left, so nav.selectedId is non-null again
  await evaluate(`(() => { const b = [...document.querySelectorAll('.c2-seg button')]
    .find((x) => x.textContent === 'Paper'); window.__paper = b; b.focus(); return document.activeElement === b; })()`);
  await pressKey('Enter');
  out.themeButtonKeptNativeEnter = await evaluate(
    `document.documentElement.getAttribute('data-theme') === 'paper' && document.activeElement === window.__paper`,
  );

  // R4: closing help restores focus to wherever it was invoked from - here, a focused Later button -
  // not a blanket default to the ? button.
  await evaluate(`(() => { const b = document.querySelector('.c2-later'); window.__r4later = b; b.focus();
    return document.activeElement === b; })()`);
  await pressKey('?');
  out.r4FocusInOverlay = await evaluate(`document.activeElement.className === 'c2-overlay-close'`);
  await pressKey('Escape');
  out.r4FocusRestoredToInvoker = await evaluate('document.activeElement === window.__r4later');
  out.r4InvokerStillConnected = await evaluate('window.__r4later.isConnected');

  // Return focus to a neutral point before the overlay/R1 checks below, exactly like an operator
  // clicking elsewhere on the page - the overlay's own focus management is what is under test next,
  // not whatever the Enter path above happened to leave focused.
  await evaluate(`(() => { document.activeElement.blur(); return true; })()`);

  // M4b: the ? button opens the keyboard overlay and moves focus into it; Escape closes it and
  // returns focus to the button that opened it. Native key events throughout (see KEYS/pressKey).
  // Element.click() does not reliably focus its target the way a real pointer click does, so this
  // focuses it explicitly first - exactly what R4's invoker-capture then needs to see.
  await evaluate(`(() => { const b = document.querySelector('.c2-keybtn'); b.focus(); b.click(); return true; })()`);
  out.overlayOpenAfterClick = await evaluate(`document.getElementById('c2-keymap').className.indexOf('is-open') >= 0`);
  out.focusInOverlayAfterClick = await evaluate(`document.activeElement.className === 'c2-overlay-close'`);

  // R1: Tab and Shift+Tab must never escape the modal overlay - with only one focusable control
  // inside it (Close), both must land right back on it, never on the "Classic view" link beneath.
  const pathBefore = await evaluate('location.pathname');
  await pressKey('Tab');
  out.focusAfterNativeTab = await evaluate(`document.activeElement.className === 'c2-overlay-close'`);
  await pressKey('Tab', { shift: true });
  out.focusAfterNativeShiftTab = await evaluate(`document.activeElement.className === 'c2-overlay-close'`);
  out.backgroundInertWhileOpen = await evaluate(
    `document.getElementById('c2-stream').hasAttribute('inert') && document.getElementById('c2-header').hasAttribute('inert')`,
  );

  // R1: a native Enter on the (correctly still-focused) Close button closes the dialog - it must
  // never reach a link outside it and navigate away.
  await pressKey('Enter');
  out.overlayClosedAfterNativeEnterOnClose = await evaluate(`document.getElementById('c2-keymap').className.indexOf('is-open') < 0`);
  out.pathUnchangedAfterNativeEnter = (await evaluate('location.pathname')) === pathBefore;
  out.backgroundInertRemovedAfterClose = await evaluate(`!document.getElementById('c2-stream').hasAttribute('inert')`);

  // Reopen and close with a native Escape too (both documented ways to close still work).
  await evaluate(`(() => { const b = document.querySelector('.c2-keybtn'); b.focus(); b.click(); return true; })()`);
  await pressKey('Escape');
  out.overlayClosedAfterNativeEscape = await evaluate(`document.getElementById('c2-keymap').className.indexOf('is-open') < 0`);
  out.focusBackOnKeysBtnAfterEscape = await evaluate(`document.activeElement === document.querySelector('.c2-keybtn')`);

  // F1: scroll up, let ordinary redraws happen (the fake server ages its cards on every read) -
  // wait until one is actually observed (see waitForAgeChange) rather than a fixed sleep. The
  // thread capture, the scrollTop=250 write and the age-baseline read are ONE atomic evaluation
  // (dev-4 finding F1): two separate CDP round-trips leave a window where a redraw lands BETWEEN
  // reading the baseline and writing scrollTop - that redraw's own age change already satisfies
  // waitForAgeChange instantly, so a LATER redraw (the one that actually follows our own setup,
  // and so is the one whose effect on scrolling this check exists to prove) is never waited for
  // or observed at all.
  const ageAtScroll = await evaluate(`(() => {
    window.__thread = document.querySelector('.c2-thread');
    window.__thread.scrollTop = 250;
    ${sabotageScrollScript}
    return document.querySelector('${WATCHED_AGE_SELECTOR}').textContent;
  })()`);
  assertFineGrainedAge(ageAtScroll, /\d+([smhd])$/, 'the watched stream card\'s age (F1 scroll baseline)');
  out.firstRedrawWaitMs = (await waitForAgeChange(ageAtScroll)).elapsedMs;
  out.afterRedraw = await measure();
  out.threadKept = await evaluate('window.__thread === document.querySelector(".c2-thread") && window.__thread.isConnected');
  out.redrawHappened = await evaluate(
    `(() => { const a = document.querySelector('${WATCHED_AGE_SELECTOR}'); return a ? a.textContent : null; })()`,
  );
  out.railImageKept = await evaluate('window.__avatar === document.querySelector("#c2-rail .c2-avatar-img") && window.__avatar.isConnected');

  // F2: focus a Later button, let redraws happen, focus must stay put on the same element - again
  // waiting until the redraw is actually observed, bounded, rather than a fixed sleep, and again
  // the setup (focus) and the age-baseline read are ONE atomic evaluation, for the identical
  // reason as F1 above (dev-4 finding F1 applies here too, not only to the scroll baseline).
  const ageBefore = await evaluate(`(() => {
    const b = document.querySelector('.c2-later');
    window.__later = b;
    b.focus();
    return document.querySelector('${WATCHED_AGE_SELECTOR}').textContent;
  })()`);
  assertFineGrainedAge(ageBefore, /\d+([smhd])$/, 'the watched stream card\'s age (F2 focus baseline)');
  const secondRedrawWait = await waitForAgeChange(ageBefore);
  out.secondRedrawWaitMs = secondRedrawWait.elapsedMs;
  out.ageMoved = secondRedrawWait.changed;
  out.focusAfter = await evaluate(`(() => { const a = document.activeElement;
    return { tag: a.tagName, key: a.getAttribute('data-c2-focus'), same: a === window.__later, inStream: document.getElementById('c2-stream').contains(a) }; })()`);

  // F2: deferring the focused card moves focus to a deliberate place inside the stream, never <body>
  await evaluate(`(() => { document.activeElement.click(); return true; })()`);
  await sleep(300);
  out.focusAfterLater = await evaluate(`(() => { const a = document.activeElement;
    return { tag: a.tagName, key: a.getAttribute('data-c2-focus'), inStream: document.getElementById('c2-stream').contains(a) }; })()`);

  // B8: board - keyed cards reconciled in place, retaining focus and scroll, with j/k/Escape.
  // The fixture (test_console2_browser.py) serves two /api/work-board items whose last_work_event_at
  // recedes every read (the same 60x-accelerated `grown` counter as /api/attention above), so the
  // watched card's own meta line changes text on every ordinary redraw - the same proof technique
  // as F1/F2 above, applied to the board instead of the stream.
  //
  // The board specifically needs real visibility (F6's own hidden-tab pause is exactly what would
  // otherwise stall it forever): a headless target has been observed drifting back to backgrounded
  // after enough prior CDP interaction (overlay/keyboard steps above), so re-assert activation
  // right here rather than trust the very first one (top of this script) to still hold this far in.
  await activateTarget(page.id);
  if (await evaluate('document.hidden')) {
    throw new Error('the page went hidden again before the board section; target activation did not hold');
  }
  const BOARD_META_SELECTOR = '[data-c2-card="board|board-a"] .c2-board-card-meta';
  await evaluate("location.hash = '#board'");
  for (let i = 0; i < 100; i++) {
    const n = await evaluate("document.querySelectorAll('.c2-board-card').length");
    if (n >= 2) break;
    if (i === 99) {
      throw new Error('board cards never rendered; board.textContent='
        + await evaluate('document.getElementById("c2-board").textContent')
        + ' docHidden=' + await evaluate('document.hidden')
        + ' route=' + await evaluate('location.hash'));
    }
    await sleep(150);
  }
  out.boardShellSwapped = await evaluate(
    `(() => { const b = document.getElementById('c2-board'); const s = document.getElementById('c2-stream');
      return !b.hidden && s.hidden; })()`,
  );

  // j selects the first rendered card; real focus and the selected class land on it together.
  await pressKey('j');
  out.boardFirstSelectedWithFocus = await evaluate(`(() => { const c = document.querySelector('.c2-board-card');
    return !!c && c.className.indexOf('is-selected') >= 0 && document.activeElement === c
      && c.getAttribute('aria-pressed') === 'true'; })()`);

  // A second j must move focus WITH the selection onto the next card, never leave it behind.
  await pressKey('j');
  out.boardSecondJMovedFocus = await evaluate(`(() => { const cards = [...document.querySelectorAll('.c2-board-card')];
    const a = document.activeElement;
    return a.tagName === 'ARTICLE' && a.className.indexOf('is-selected') >= 0 && cards[1] === a; })()`);

  // Scroll the board container, let an ordinary redraw happen (the watched card's own age-derived
  // meta text changes every read), and check the container, the selected card and its focus all
  // survive - the atomic setup+baseline read avoids the same race F1/F2 guard against above.
  const metaAtScroll = await evaluate(`(() => {
    window.__boardEl = document.getElementById('c2-board');
    window.__boardCard = document.activeElement;
    window.__boardEl.scrollTop = 40;
    return document.querySelector('${BOARD_META_SELECTOR}').textContent;
  })()`);
  assertFineGrainedAge(metaAtScroll, /active \d+([smhd]) ago/, 'the watched board card\'s "active" age (scroll baseline)');
  await waitForTextChange(BOARD_META_SELECTOR, metaAtScroll);
  out.boardScrollKept = await evaluate('window.__boardEl.scrollTop') === 40;
  out.boardContainerKept = await evaluate('window.__boardEl === document.getElementById("c2-board") && window.__boardEl.isConnected');
  out.boardFocusKept = await evaluate('document.activeElement === window.__boardCard && window.__boardCard.isConnected');
  out.boardDetailShowsSelection = await evaluate(
    "document.getElementById('c2-board-detail').textContent.indexOf('Select a card') === -1",
  );

  // Escape clears the board selection (never navigates away or reaches the stream underneath).
  await pressKey('Escape');
  out.boardSelectionClearedByEscape = await evaluate(
    "document.querySelectorAll('.c2-board-card.is-selected').length === 0",
  );

  // F4: a card is `role="button" tabindex="0"` on a plain <article> - a real <button> gets
  // automatic Enter/Space activation from the browser itself; an ARIA role alone gets NONE, so
  // this must be wired up by the page's own key handler or native input on a focused, unselected
  // card silently does nothing (dev-4's own finding). Focus is set directly (simulating whatever
  // got it there - Tab order, not just j/k) to isolate activation from selection-and-focus-together.
  await evaluate("(() => { document.querySelector('.c2-board-card').focus(); return true; })()");
  await pressKey('Enter');
  out.boardEnterActivatesFocusedCard = await evaluate(
    "document.querySelector('.c2-board-card').className.indexOf('is-selected') >= 0",
  );
  await pressKey('Escape');
  await evaluate(
    "(() => { const c = [...document.querySelectorAll('.c2-board-card')][1]; c.focus(); return true; })()",
  );
  await pressKey(' ');
  out.boardSpaceActivatesFocusedCard = await evaluate(
    "[...document.querySelectorAll('.c2-board-card')][1].className.indexOf('is-selected') >= 0",
  );

  // N2 (PR #221 delta round 2): still on #board, a native Enter on the (real, server-rendered)
  // Conversation link must actually navigate there - the board's own Enter/Space handler must
  // never suppress a key it did not itself handle, whatever else is focused on this route.
  await evaluate(
    "(() => { const a = document.querySelector('[data-c2-route=\"conversation\"]'); a.focus();"
    + ' return document.activeElement === a; })()',
  );
  await pressKey('Enter');
  await sleep(200);
  out.n2ConversationLinkNavigatesOnEnter = await evaluate("location.hash === '#conversation'");
  // Back to #board for a clean end state, and confirm the help button's native Enter still works too.
  await evaluate("location.hash = '#board'");
  await sleep(200);
  await evaluate("(() => { document.querySelector('.c2-keybtn').focus(); return true; })()");
  await pressKey('Enter');
  out.n2HelpButtonStillOpensOnEnter = await evaluate(
    "document.getElementById('c2-keymap').className.indexOf('is-open') >= 0",
  );
  await pressKey('Escape');

  out.problems = problems;
  process.stdout.write(JSON.stringify(out));
} finally {
  try { ws && ws.close(); } catch (e) { /* closing */ }
  child.kill();
}
process.exit(0);
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  main();
}
