// Real-browser check for the console v2 stream, driven over the DevTools protocol.
// Usage: node console2_browser_check.mjs <browser-exe> <page-url> <profile-dir>
// Prints one JSON object of measurements; tests/test_console2_browser.py asserts on it.
//
// What the DOM stub cannot show and this does: a detached element has no scroll layout (the thread
// must be scrolled after insertion), and a redraw that replaces a focused control drops focus to <body>.
import { spawn } from 'node:child_process';

const [exe, pageUrl, profile] = process.argv.slice(2);
const PORT = 9300 + Math.floor(Math.random() * 500);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(exe, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', `--user-data-dir=${profile}`,
  `--remote-debugging-port=${PORT}`, '--window-size=1240,780', 'about:blank',
], { stdio: 'ignore' });

async function json(path) {
  for (let i = 0; i < 100; i++) {
    try { const r = await fetch(`http://127.0.0.1:${PORT}${path}`); if (r.ok) return await r.json(); } catch (e) { /* not up yet */ }
    await sleep(100);
  }
  throw new Error('browser did not start');
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
  await json('/json/version');
  const targets = await json('/json/list');
  const page = targets.find((t) => t.type === 'page');
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
  await send('Page.navigate', { url: pageUrl });
  await sleep(3500);                                   // first poll, feeds, first draw

  const out = {};
  const measure = () => evaluate(`(() => { const t = document.querySelector('.c2-thread');
    return t ? { top: t.scrollTop, height: t.scrollHeight, client: t.clientHeight, connected: t.isConnected } : null; })()`);

  // F1: first draw is scrolled to the end
  out.initial = await measure();

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

  // F1: scroll up, let ordinary redraws happen (the fake server ages its cards on every read)
  await evaluate(`(() => { window.__thread = document.querySelector('.c2-thread'); window.__thread.scrollTop = 250; return true; })()`);
  await sleep(4500);
  out.afterRedraw = await measure();
  out.threadKept = await evaluate('window.__thread === document.querySelector(".c2-thread") && window.__thread.isConnected');
  out.redrawHappened = await evaluate(`(() => { const a = document.querySelector('.c2-age'); return a ? a.textContent : null; })()`);
  out.railImageKept = await evaluate('window.__avatar === document.querySelector("#c2-rail .c2-avatar-img") && window.__avatar.isConnected');

  // F2: focus a Later button, let redraws happen, focus must stay put on the same element
  await evaluate(`(() => { const b = document.querySelector('.c2-later'); window.__later = b; b.focus();
    return document.activeElement === b; })()`);
  const ageBefore = await evaluate(`document.querySelector('.c2-age').textContent`);
  await sleep(4500);
  out.ageMoved = ageBefore !== await evaluate(`document.querySelector('.c2-age').textContent`);
  out.focusAfter = await evaluate(`(() => { const a = document.activeElement;
    return { tag: a.tagName, key: a.getAttribute('data-c2-focus'), same: a === window.__later, inStream: document.getElementById('c2-stream').contains(a) }; })()`);

  // F2: deferring the focused card moves focus to a deliberate place inside the stream, never <body>
  await evaluate(`(() => { document.activeElement.click(); return true; })()`);
  await sleep(300);
  out.focusAfterLater = await evaluate(`(() => { const a = document.activeElement;
    return { tag: a.tagName, key: a.getAttribute('data-c2-focus'), inStream: document.getElementById('c2-stream').contains(a) }; })()`);

  out.problems = problems;
  process.stdout.write(JSON.stringify(out));
} finally {
  try { ws && ws.close(); } catch (e) { /* closing */ }
  child.kill();
}
process.exit(0);
