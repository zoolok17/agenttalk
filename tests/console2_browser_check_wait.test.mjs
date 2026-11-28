// Issue #229 / PR #230 delta review (F1-F3): the DevTools startup/page-target waits factored out
// of console2_browser_check.mjs, tested against a stub - no real browser, process or timers
// needed. A fake clock (`now`/`sleep` are both injectable) makes a "90 seconds elapsed, nothing
// ever answered" case run instantly while still exercising the real bounded-retry logic.
// Run: node tests/console2_browser_check_wait.test.mjs   (also run by tests/test_console2_web.py)
import assert from 'node:assert/strict';
import { createRunner } from './console2_harness.mjs';
import {
  FatalExitError, makeDeadline, waitForDeadline, waitForPageTarget,
  parseDevToolsActivePort, browserLaunchArgs, assertOwnEndpoint, NotThisBrowserError,
  startDevToolsSession, PER_REQUEST_CAP_MS, withNoAnswerLimit, NO_ANSWER_MARK,
} from './console2_browser_check.mjs';

const { test, run } = createRunner('console2 browser check: deadline/page-target waits');

function fakeClock(startMs = 0) {
  let t = startMs;
  return { now: () => t, sleep: async (ms) => { t += ms; } };
}

// --------------------------------------------------------------------------- waitForDeadline

test('waitForDeadline: retries a thrown error (not-ready-yet) until fn eventually resolves', async () => {
  const clock = fakeClock();
  let calls = 0;
  const result = await waitForDeadline(async () => {
    calls += 1;
    if (calls < 3) throw new Error('ECONNREFUSED (simulated: not up yet)');
    return 'ready';
  }, { deadline: makeDeadline(10000, clock), intervalMs: 1, sleep: clock.sleep });
  assert.equal(result, 'ready');
  assert.equal(calls, 3);
});

test('waitForDeadline: retries while fn resolves undefined ("not yet", not an error)', async () => {
  const clock = fakeClock();
  let calls = 0;
  const result = await waitForDeadline(async () => {
    calls += 1;
    return calls < 4 ? undefined : 42;
  }, { deadline: makeDeadline(10000, clock), intervalMs: 1, sleep: clock.sleep });
  assert.equal(result, 42);
  assert.equal(calls, 4);
});

test('waitForDeadline: bounded - a condition that never resolves still raises, never waits forever', async () => {
  const clock = fakeClock();
  let calls = 0;
  await assert.rejects(
    () => waitForDeadline(async () => { calls += 1; return undefined; }, {
      deadline: makeDeadline(90000, clock), intervalMs: 150, describe: 'the thing', sleep: clock.sleep,
    }),
    /the thing did not happen within the shared budget/,
  );
  assert.ok(calls > 1, 'it actually retried, not just checked once');
  assert.ok(clock.now() >= 90000, 'the fake clock really did advance to the deadline');
});

test('waitForDeadline: checkExited fails FAST and clearly - never waits out the rest of the budget', async () => {
  const clock = fakeClock();
  let calls = 0;
  await assert.rejects(
    () => waitForDeadline(async () => { calls += 1; return undefined; }, {
      deadline: makeDeadline(90000, clock), intervalMs: 150, describe: 'the browser answering',
      checkExited: () => (calls >= 2 ? { code: 1, signal: null } : null), sleep: clock.sleep,
    }),
    /the browser answering: the process exited before it was met/,
  );
  assert.equal(calls, 2, 'stopped at the exit, not after riding out the whole timeout');
  assert.ok(clock.now() < 90000, 'did not wait out the deadline - failed fast instead');
});

test('N1 regression (connector P2): a deadline-expired failure includes accumulated stderr, like the fatal-exit path does', async () => {
  // The browser can stay ALIVE (no exit -> no FatalExitError) while never exposing the DevTools
  // endpoint - exactly the slow-startup path this whole change exists to diagnose. The old,
  // pre-#229 fixed-attempt-budget error always included the accumulated stderr; this path must too.
  const clock = fakeClock();
  await assert.rejects(
    () => waitForDeadline(async () => undefined, {
      deadline: makeDeadline(5000, clock), intervalMs: 150, describe: 'the thing', sleep: clock.sleep,
      getDiagnostics: () => 'stderr:\nheadless dbus noise',
    }),
    /the thing did not happen within the shared budget; stderr:\nheadless dbus noise/,
  );
});

// -------------------------------------------------------- PR #230 delta review, F2: one budget

test('F2 regression: two waits sharing ONE deadline stay bounded by that ONE budget, not doubled', async () => {
  // Reproduces the reviewer's own finding in shape: an OUTER wait (like waitForPageTarget) whose
  // fn is itself ANOTHER wait (like the old json()) that never resolves. On fee53bf each level
  // created its OWN fresh timeoutMs/now pair - the reviewer's fake-clock composition measured
  // 180,050 ms (two independent 90s budgets). Sharing ONE `deadline` object between both levels,
  // as the fix requires, must keep the TOTAL elapsed time within that ONE budget.
  const clock = fakeClock();
  const deadline = makeDeadline(90000, clock);
  const outer = waitForDeadline(
    () => waitForDeadline(async () => undefined, { deadline, sleep: clock.sleep, intervalMs: 150 }),
    { deadline, sleep: clock.sleep, intervalMs: 150 },
  );
  await assert.rejects(() => outer);
  assert.ok(clock.now() < 91000, `expected ~90s total, got ${clock.now()}ms - looks like two independent budgets`);
});

// PR #230 delta review round 2, N1 (connector, major): the retry loop used to RACE each attempt
// against one poll-interval tick so a never-settling `fn()` could not hang the loop. That race was
// exactly the bug - a fast attempt always "won", so the tick's sleep was merely started, never
// actually awaited, and intervalMs throttled nothing (reviewer-1 measured 14,109 real attempts in
// 51ms for an immediate-undefined fn, 5,858 in 50ms for an immediate rejection). The recast drops
// the race: fn is now awaited fully, every time, and the CONTRACT is that fn bounds itself (its own
// AbortSignal.timeout, like makeFetchJson) - so a never-settling raw promise is no longer this
// loop's problem to solve; it is fn's, by actually releasing the resource it is waiting on. Real
// timers here (not the fake clock above) because the eager fake `sleep` mutates its clock the
// instant it is CALLED, independent of whether it is actually awaited - exactly what hid this bug
// from the fake-clock suite in the first place; only real elapsed time proves the pacing is real.
test('N1 pacing (round 2): intervalMs actually paces immediate-undefined attempts, not thousands per tick', async () => {
  let calls = 0;
  const budgetMs = 300;
  const intervalMs = 50;
  await assert.rejects(
    () => waitForDeadline(async () => { calls += 1; return undefined; }, {
      deadline: makeDeadline(budgetMs), intervalMs, describe: 'the thing',
    }),
    /the thing did not happen within the shared budget/,
  );
  const maxExpected = Math.ceil(budgetMs / intervalMs) + 1;
  assert.ok(calls <= maxExpected,
    `expected at most ~${maxExpected} attempts over ${budgetMs}ms at ${intervalMs}ms pacing, got ${calls} - intervalMs is not throttling`);
});

test('N1 pacing (round 2): intervalMs actually paces immediate-rejection attempts too', async () => {
  let calls = 0;
  const budgetMs = 300;
  const intervalMs = 50;
  await assert.rejects(
    () => waitForDeadline(async () => { calls += 1; throw new Error('ECONNREFUSED (simulated)'); }, {
      deadline: makeDeadline(budgetMs), intervalMs, describe: 'the thing',
    }),
    /the thing did not happen within the shared budget/,
  );
  const maxExpected = Math.ceil(budgetMs / intervalMs) + 1;
  assert.ok(calls <= maxExpected,
    `expected at most ~${maxExpected} attempts over ${budgetMs}ms at ${intervalMs}ms pacing, got ${calls} - intervalMs is not throttling`);
});

test('N1 (round 2): the abort wiring a real fn() relies on (makeFetchJson\'s AbortSignal.timeout pattern) actually aborts a never-answering attempt at its cap, rather than hanging', async () => {
  // makeFetchJson bounds every real HTTP attempt via fetch(url, { signal: AbortSignal.timeout(capMs) })
  // - this is what makes "fn must bound itself" a safe contract for waitForDeadline to rely on. Proven
  // here against a stub that never answers unless aborted, exercising the exact same signal wiring.
  const capMs = 30;
  // AbortSignal.timeout()'s own internal timer is unref'd (by design, so a live abort signal never
  // alone keeps a process alive) - unlike a real fetch(), this bare stub holds no other handle, so
  // with nothing else pending Node would exit clean before the timer ever fires. A real fn() never
  // hits this: the pending socket itself keeps the loop alive. A plain ref'd keep-alive reproduces
  // that here, without changing what is actually being proven (the abort itself still fires the cap).
  const keepAlive = setTimeout(() => {}, capMs + 500);
  try {
    const neverAnswers = (signal) => new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('This operation was aborted', 'AbortError')));
    });
    const start = Date.now();
    await assert.rejects(
      () => neverAnswers(AbortSignal.timeout(capMs)),
      (err) => err.name === 'AbortError',
    );
    const elapsed = Date.now() - start;
    assert.ok(elapsed < 1000, `expected the abort at ~${capMs}ms to fire promptly, took ${elapsed}ms`);
  } finally {
    clearTimeout(keepAlive);
  }
});

// -------------------------------------------------------- PR #230 delta review, F3: exit propagation

test('F3 regression: a FatalExitError raised inside fn propagates immediately, never swallowed as "not ready yet"', async () => {
  const clock = fakeClock();
  const deadline = makeDeadline(90000, clock);
  let calls = 0;
  await assert.rejects(
    () => waitForDeadline(async () => {
      calls += 1;
      if (calls === 1) return undefined;   // one ordinary "not ready yet" round first
      throw new FatalExitError('the browser', { code: 1, signal: null, stderr: 'dbus noise' });
    }, { deadline, sleep: clock.sleep, intervalMs: 150 }),
    (err) => err instanceof FatalExitError && err.exitInfo.code === 1 && err.exitInfo.stderr === 'dbus noise',
  );
  assert.equal(calls, 2, 'stopped immediately at the fatal exit, never retried further');
  assert.ok(clock.now() < 90000, 'never waited out the full budget for something already fatal');
});

test('F3 regression: a fatal exit from a nested wait (like the old listTargets) propagates through waitForPageTarget too', async () => {
  const clock = fakeClock();
  const deadline = makeDeadline(90000, clock);
  await assert.rejects(
    () => waitForPageTarget(async () => {
      throw new FatalExitError('the DevTools endpoint (/json/list)', { code: 1, signal: null, stderr: 'boom' });
    }, { deadline, sleep: clock.sleep }),
    (err) => err instanceof FatalExitError && err.exitInfo.stderr === 'boom',
  );
  assert.ok(clock.now() < 1000, 'failed on the very first attempt - never retried a fatal exit');
});

// ------------------------------------------------------------------------- waitForPageTarget

test('waitForPageTarget: resolves once a type==="page" target actually appears in the list', async () => {
  const clock = fakeClock();
  let calls = 0;
  const listTargetsOnce = async () => {
    calls += 1;
    if (calls === 1) return [{ type: 'background_page', id: 'bg' }];   // a real, non-page target first
    if (calls === 2) return [];                                        // then briefly empty
    return [{ type: 'background_page', id: 'bg' }, { type: 'page', id: 'the-page', webSocketDebuggerUrl: 'ws://x' }];
  };
  const page = await waitForPageTarget(listTargetsOnce, { deadline: makeDeadline(10000, clock), intervalMs: 1, sleep: clock.sleep });
  assert.deepEqual(page, { type: 'page', id: 'the-page', webSocketDebuggerUrl: 'ws://x' });
  assert.equal(calls, 3);
});

test('waitForPageTarget: a list that never grows a page target still raises, bounded, with a clear message', async () => {
  const clock = fakeClock();
  await assert.rejects(
    () => waitForPageTarget(async () => [{ type: 'background_page', id: 'bg' }],
      { deadline: makeDeadline(90000, clock), intervalMs: 150, sleep: clock.sleep }),
    /a page target appearing in \/json\/list did not happen within the shared budget/,
  );
  assert.ok(clock.now() >= 90000);
});

test('waitForPageTarget: a listTargetsOnce that throws (endpoint transiently unready) is treated as not-yet, not fatal', async () => {
  const clock = fakeClock();
  let calls = 0;
  const page = await waitForPageTarget(async () => {
    calls += 1;
    if (calls === 1) throw new Error('ECONNREFUSED (simulated)');
    return [{ type: 'page', id: 'p' }];
  }, { deadline: makeDeadline(10000, clock), intervalMs: 1, sleep: clock.sleep });
  assert.equal(page.id, 'p');
  assert.equal(calls, 2);
});

const browserPath = '/devtools/browser/12345678-1234-5678-abcd-123456789abc';
const file = `52568\n${browserPath}`;
const info = { port: 52568, browserPath };
const ownVersion = { webSocketDebuggerUrl: `ws://127.0.0.1:52568${browserPath}` };
const page = { type: 'page', id: 'own-page', webSocketDebuggerUrl: 'ws://page' };

test('A1: complete browser files accept LF, CRLF and an optional final newline', () => {
  for (const text of [file, file + '\n', file.replace('\n', '\r\n'), file.replace('\n', '\r\n') + '\r\n']) {
    assert.deepEqual(parseDevToolsActivePort(text), info);
  }
  const upper = browserPath.toUpperCase().replace('/DEVTOOLS/BROWSER/', '/devtools/browser/');
  assert.deepEqual(parseDevToolsActivePort(`52568\n${upper}`), { port: 52568, browserPath: upper });
});

test('A2: every partial browser-file prefix is not ready', () => {
  for (let length = 0; length < file.length; length++) {
    assert.equal(parseDevToolsActivePort(file.slice(0, length)), undefined, `prefix ${length}`);
  }
  assert.equal(parseDevToolsActivePort(undefined), undefined);
});

test('A3: invalid ports, paths and extra lines are not complete files', () => {
  for (const port of ['0', '65536', '012', ' 1', '12a', '-1']) {
    assert.equal(parseDevToolsActivePort(`${port}\n${browserPath}`), undefined);
  }
  for (const path of ['/devtools/browser/not-a-uuid', browserPath + '?x', '/other/' + browserPath, browserPath + '#x']) {
    assert.equal(parseDevToolsActivePort(`52568\n${path}`), undefined);
  }
  assert.equal(parseDevToolsActivePort(file + '\nextra'), undefined);
  assert.equal(parseDevToolsActivePort(file + '\n\n'), undefined);
});

test('A4: launch asks the browser for exactly one fresh port', () => {
  const args = browserLaunchArgs('own profile', ['--headless=new', '--disable-gpu']);
  assert.deepEqual(args.slice(0, 2), ['--headless=new', '--disable-gpu']);
  assert.deepEqual(args.filter((arg) => arg.startsWith('--remote-debugging-port=')), ['--remote-debugging-port=0']);
  assert.ok(args.includes('--user-data-dir=own profile'));
  assert.ok(args.includes('--window-size=1240,780') && args.includes('about:blank'));
});

test('B1/B2: ownership requires the exact parsed browser endpoint', () => {
  assert.doesNotThrow(() => assertOwnEndpoint(ownVersion, info));
  const urls = [
    `ws://127.0.0.1:52568${browserPath.replace('123456789abc', '123456789abd')}`,
    `ws://127.0.0.1:52568/devtools/browser/OTHER?copied=${browserPath}`,
    ownVersion.webSocketDebuggerUrl + '#fragment', ownVersion.webSocketDebuggerUrl + '?x',
    ownVersion.webSocketDebuggerUrl.replace('52568', '52569'),
    ownVersion.webSocketDebuggerUrl.replace('ws:', 'wss:'),
    ownVersion.webSocketDebuggerUrl.replace('127.0.0.1', 'localhost'),
    ownVersion.webSocketDebuggerUrl.replace('127.0.0.1', '[::1]'),
    'not-a-url' + browserPath, '', 12, null,
  ];
  for (const value of [...urls.map((url) => ({ webSocketDebuggerUrl: url })), {}, null, 'bad']) {
    assert.throws(() => assertOwnEndpoint(value, info),
      (error) => error instanceof NotThisBrowserError && /52568.*different program/.test(error.message));
  }
});

function sessionDeps(clock, overrides = {}) {
  return {
    readActivePort: async () => file,
    fetchJson: async (url) => url.endsWith('/json/version') ? ownVersion : [page],
    fetchText: async () => ({ ok: true, status: 200, body: 'activated' }),
    deadline: makeDeadline(90000, clock), sleep: clock.sleep, checkExited: () => null,
    getDiagnostics: () => 'file diagnostic', ...overrides,
  };
}

async function withFakeDate(clock, action) {
  // Also expose the injected clock to an accidentally renewed makeDeadline().
  const realNow = Date.now;
  Date.now = clock.now;
  try { return await action(); } finally { Date.now = realNow; }
}

test('C1: the startup seam shares the discovered base and caps every activation', async () => {
  const clock = fakeClock();
  const texts = [undefined, undefined, file.slice(0, 6), file.slice(0, -1), file];
  const urls = [], actions = [];
  let reads = 0, versions = 0, lists = 0, failAction = false;
  const session = await startDevToolsSession(sessionDeps(clock, {
    readActivePort: async () => { reads++; return texts.shift(); },
    fetchJson: async (url) => {
      urls.push(url);
      if (url.endsWith('/json/version')) {
        if (++versions === 1) throw new Error('not ready');
        return ownVersion;
      }
      return ++lists === 1 ? [] : [page];
    },
    fetchText: async (url, capMs) => {
      actions.push([url, capMs]);
      return failAction ? { ok: false, status: 500, body: 'x' } : { ok: true, status: 200, body: 'ok' };
    },
  }));
  assert.equal(session.base, 'http://127.0.0.1:52568');
  assert.deepEqual(session.info, info);
  assert.deepEqual(session.page, page);
  assert.equal(reads, 5);
  assert.ok(urls.length >= 4 && urls.every((url) => url.startsWith(session.base + '/')));
  await session.activate(page.id);
  await session.activate(page.id);
  assert.deepEqual(actions, Array(2).fill([`${session.base}/json/activate/own-page`, PER_REQUEST_CAP_MS]));
  failAction = true;
  await assert.rejects(() => session.activate(page.id), /could not activate the browser target: http 500 x/);
  assert.equal(actions.at(-1)[1], PER_REQUEST_CAP_MS);
  assert.equal(reads, 5, 'the file is read only until it is complete');
});

test('C2: a wrong browser is fatal before any page request or retry', async () => {
  const clock = fakeClock();
  let pages = 0;
  await assert.rejects(() => startDevToolsSession(sessionDeps(clock, {
    fetchJson: async (url) => {
      if (url.endsWith('/json/version')) return { webSocketDebuggerUrl: 'ws://other' };
      pages++;
      return [page];
    },
  })), NotThisBrowserError);
  assert.equal(clock.now(), 0);
  assert.equal(pages, 0);
});

for (const phase of ['file', 'version']) {
  test(`C3: exit during ${phase} stops on the next round`, async () => {
    const clock = fakeClock();
    await assert.rejects(() => startDevToolsSession(sessionDeps(clock, {
      readActivePort: async () => phase === 'file' ? undefined : file,
      fetchJson: async () => undefined,
      checkExited: () => clock.now() >= 150 ? { code: 7, stderr: 'gone' } : null,
    })), (error) => error instanceof FatalExitError && error.exitInfo.code === 7);
    assert.equal(clock.now(), 150);
  });
}

for (const phase of ['file-spent', 'version', 'page', 'file-zero']) {
  test(`C4: ${phase} ends at the absolute shared deadline`, async () => {
    const clock = fakeClock();
    await withFakeDate(clock, async () => {
      const deps = sessionDeps(clock);
      if (phase === 'file-spent') await clock.sleep(60000);
      deps.readActivePort = async () => {
        if (phase.startsWith('file')) return undefined;
        await clock.sleep(Math.max(0, 60000 - clock.now()));
        return file;
      };
      deps.fetchJson = async (url) => {
        if (phase === 'version') return undefined;
        if (url.endsWith('/json/version')) {
          await clock.sleep(Math.max(0, 80000 - clock.now()));
          return ownVersion;
        }
        return [];
      };
      const description = phase.startsWith('file') ? /DevToolsActivePort/ : phase === 'version' ? /\/json\/version/ : /\/json\/list/;
      await assert.rejects(() => startDevToolsSession(deps), (error) => {
        assert.match(error.message, description);
        assert.match(error.message, /file diagnostic/);
        return true;
      });
      assert.equal(clock.now(), 90000);
    });
  });
}

test('C5: an ordinary file read error is retried', async () => {
  const clock = fakeClock();
  let reads = 0;
  const session = await startDevToolsSession(sessionDeps(clock, {
    readActivePort: async () => {
      if (++reads === 1) throw Object.assign(new Error('read error'), { code: 'EACCES' });
      return file;
    },
  }));
  assert.equal(session.page.id, page.id);
  assert.equal(reads, 2);
  assert.equal(clock.now(), 150);
});

// #944: a start-up step the browser never answers ends with a named error (a fake timer: no real time passes).
test('withNoAnswerLimit: a step that never settles rejects with the marker; one that settles passes through', async () => {
  let fire;
  const timers = { setT: (fn) => { fire = fn; return 1; }, clearT: () => {} };
  const stuck = withNoAnswerLimit(new Promise(() => {}), 1000, 'opening the socket', timers);
  fire();
  await assert.rejects(stuck, (e) => e.message.startsWith(NO_ANSWER_MARK) && e.message.includes('opening the socket'));
  assert.equal(await withNoAnswerLimit(Promise.resolve('up'), 1000, 'x', timers), 'up');
  await assert.rejects(withNoAnswerLimit(Promise.reject(new Error('boom')), 1000, 'x', timers), /boom/);
});

run();
