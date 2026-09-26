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

  // M4b: the ? button opens the keyboard overlay and moves focus into it; Escape closes it and
  // returns focus to the button that opened it.
  await evaluate(`(() => { document.querySelector('.c2-keybtn').click(); return true; })()`);
  out.overlayOpenAfterClick = await evaluate(`document.getElementById('c2-keymap').className.indexOf('is-open') >= 0`);
  out.focusInOverlayAfterClick = await evaluate(`document.activeElement.className === 'c2-overlay-close'`);
  await evaluate(`(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); return true; })()`);
  out.overlayClosedAfterEscape = await evaluate(`document.getElementById('c2-keymap').className.indexOf('is-open') < 0`);
  out.focusBackOnKeysBtnAfterEscape = await evaluate(`document.activeElement === document.querySelector('.c2-keybtn')`);

  // M4b: one key path - j selects the first card, Enter focuses its first action (Later, since
  // these fixture cards carry no options at all).
  await evaluate(`(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'j', bubbles: true })); return true; })()`);
  out.firstCardSelectedAfterJ = await evaluate(
    `(() => { const c = document.querySelector('.c2-card'); return !!c && c.className.indexOf('is-selected') >= 0; })()`,
  );
  await evaluate(`(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); return true; })()`);
  out.focusAfterEnterOnSelected = await evaluate(`(() => { const a = document.activeElement;
    return { tag: a.tagName, cls: a.className, sameAsFirstCardLater: a === document.querySelector('.c2-card .c2-later') }; })()`);

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
