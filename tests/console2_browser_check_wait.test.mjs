// Issue #229 / PR #230 delta review (F1-F3): the DevTools startup/page-target waits factored out
// of console2_browser_check.mjs, tested against a stub - no real browser, process or timers
// needed. A fake clock (`now`/`sleep` are both injectable) makes a "90 seconds elapsed, nothing
// ever answered" case run instantly while still exercising the real bounded-retry logic.
// Run: node tests/console2_browser_check_wait.test.mjs   (also run by tests/test_console2_web.py)
import assert from 'node:assert/strict';
import { createRunner } from './console2_harness.mjs';
import {
  FatalExitError, makeDeadline, waitForDeadline, waitForPageTarget,
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

test('F2 regression: a never-settling fn() does not hang the loop past the shared deadline', async () => {
  const clock = fakeClock();
  const deadline = makeDeadline(5000, clock);
  await assert.rejects(
    () => waitForDeadline(() => new Promise(() => { /* never settles */ }),
      { deadline, sleep: clock.sleep, intervalMs: 150 }),
    /did not happen within the shared budget/,
  );
  assert.ok(clock.now() >= 5000, 'the deadline elapsed rather than hanging forever on fn()');
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

run();
