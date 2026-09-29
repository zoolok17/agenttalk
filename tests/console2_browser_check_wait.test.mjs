// Issue #229 regression: the DevTools startup/page-target waits factored out of
// console2_browser_check.mjs, tested against a stub - no real browser, process or timers needed.
// A fake clock (`now`/`sleep` are both injectable) makes a "90 seconds elapsed, nothing ever
// answered" case run instantly while still exercising the real bounded-retry logic.
// Run: node tests/console2_browser_check_wait.test.mjs   (also run by tests/test_console2_web.py)
import assert from 'node:assert/strict';
import { createRunner } from './console2_harness.mjs';
import { waitForDeadline, waitForPageTarget } from './console2_browser_check.mjs';

const { test, run } = createRunner('console2 browser check: deadline/page-target waits');

function fakeClock(startMs = 0) {
  let t = startMs;
  return { now: () => t, sleep: async (ms) => { t += ms; } };
}

// --------------------------------------------------------------------------- waitForDeadline

test('waitForDeadline: retries a thrown error (not-ready-yet) until fn eventually resolves', async () => {
  let calls = 0;
  const result = await waitForDeadline(async () => {
    calls += 1;
    if (calls < 3) throw new Error('ECONNREFUSED (simulated: not up yet)');
    return 'ready';
  }, { timeoutMs: 10000, intervalMs: 1, ...fakeClock() });
  assert.equal(result, 'ready');
  assert.equal(calls, 3);
});

test('waitForDeadline: retries while fn resolves undefined ("not yet", not an error)', async () => {
  let calls = 0;
  const result = await waitForDeadline(async () => {
    calls += 1;
    return calls < 4 ? undefined : 42;
  }, { timeoutMs: 10000, intervalMs: 1, ...fakeClock() });
  assert.equal(result, 42);
  assert.equal(calls, 4);
});

test('waitForDeadline: bounded - a condition that never resolves still raises, never waits forever', async () => {
  const clock = fakeClock();
  let calls = 0;
  await assert.rejects(
    () => waitForDeadline(async () => { calls += 1; return undefined; },
      { timeoutMs: 90000, intervalMs: 150, describe: 'the thing', ...clock }),
    /the thing did not happen within 90s/,
  );
  assert.ok(calls > 1, 'it actually retried, not just checked once');
  assert.ok(clock.now() >= 90000, 'the fake clock really did advance to the deadline');
});

test('waitForDeadline: checkExited fails FAST and clearly - never waits out the rest of the budget', async () => {
  const clock = fakeClock();
  let calls = 0;
  await assert.rejects(
    () => waitForDeadline(async () => { calls += 1; return undefined; }, {
      timeoutMs: 90000, intervalMs: 150, describe: 'the browser answering',
      checkExited: () => (calls >= 2 ? { code: 1, signal: null } : null),
      ...clock,
    }),
    /the browser answering: the process exited before it was met/,
  );
  assert.equal(calls, 2, 'stopped at the exit, not after riding out the whole timeout');
  assert.ok(clock.now() < 90000, 'did not wait out the deadline - failed fast instead');
});

// ------------------------------------------------------------------------- waitForPageTarget

test('waitForPageTarget: resolves once a type==="page" target actually appears in the list', async () => {
  let calls = 0;
  const listTargets = async () => {
    calls += 1;
    if (calls === 1) return [{ type: 'background_page', id: 'bg' }];   // a real, non-page target first
    if (calls === 2) return [];                                        // then briefly empty
    return [{ type: 'background_page', id: 'bg' }, { type: 'page', id: 'the-page', webSocketDebuggerUrl: 'ws://x' }];
  };
  const page = await waitForPageTarget(listTargets, { intervalMs: 1, ...fakeClock() });
  assert.deepEqual(page, { type: 'page', id: 'the-page', webSocketDebuggerUrl: 'ws://x' });
  assert.equal(calls, 3);
});

test('waitForPageTarget: a list that never grows a page target still raises, bounded, with a clear message', async () => {
  const clock = fakeClock();
  await assert.rejects(
    () => waitForPageTarget(async () => [{ type: 'background_page', id: 'bg' }], { intervalMs: 150, ...clock }),
    /a page target appearing in \/json\/list did not happen within 90s/,
  );
  assert.ok(clock.now() >= 90000);
});

test('waitForPageTarget: a listTargets that throws (endpoint transiently unready) is treated as not-yet, not fatal', async () => {
  let calls = 0;
  const page = await waitForPageTarget(async () => {
    calls += 1;
    if (calls === 1) throw new Error('ECONNREFUSED (simulated)');
    return [{ type: 'page', id: 'p' }];
  }, { intervalMs: 1, ...fakeClock() });
  assert.equal(page.id, 'p');
  assert.equal(calls, 2);
});

run();
