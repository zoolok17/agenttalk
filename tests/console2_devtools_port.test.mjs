// Real adapters against listeners owned by this test; no browser or external service.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createRunner } from './console2_harness.mjs';
import {
  startDevToolsSession, makeReadActivePort, makeFetchJson, makeFetchText,
  makeDeadline, NotThisBrowserError, clearActivePort,
} from './console2_browser_check.mjs';

const { test, run } = createRunner('console2 DevTools port');
const browserPath = '/devtools/browser/12345678-1234-5678-abcd-123456789abc';

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  return server.address().port;
}

async function close(server, sockets) {
  for (const socket of sockets) socket.destroy();
  await new Promise((resolve) => server.close(resolve));
}

function connections(server) {
  const sockets = new Set();
  server.on('connection', (socket) => {
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
  });
  return sockets;
}

async function beforeGuard(promise, ms, message) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error(message)), ms);
    })]);
  } finally {
    clearTimeout(timer);
  }
}

for (const own of [false, true]) {
  test(own ? 'D2/D4: own endpoint, real activation and HTTP errors' : 'D1: another browser is refused before page lookup', async () => {
    const paths = [];
    let port, activationStatus = 200;
    const page = { type: 'page', id: 'own-page', webSocketDebuggerUrl: 'ws://page' };
    const server = http.createServer((req, res) => {
      paths.push([req.method, req.url]);
      if (req.url === '/json/version') {
        res.end(JSON.stringify({ webSocketDebuggerUrl: `ws://127.0.0.1:${port}${own ? browserPath : browserPath.replace('123456789abc', '123456789abd')}` }));
      } else if (req.url === '/json/list') {
        res.end(JSON.stringify([page]));
      } else {
        res.writeHead(activationStatus, { 'Content-Type': 'text/plain' });
        res.end('activation body');
      }
    });
    const sockets = connections(server);
    const profile = await fs.mkdtemp(join(os.tmpdir(), 'devtools-port-'));
    try {
      port = await listen(server);
      await fs.writeFile(join(profile, 'DevToolsActivePort'), `${port}\n${browserPath}`);
      const start = startDevToolsSession({
        readActivePort: makeReadActivePort(fs, profile), fetchJson: makeFetchJson(fetch),
        fetchText: makeFetchText(fetch), deadline: makeDeadline(2000),
      });
      if (!own) {
        await assert.rejects(() => start, NotThisBrowserError);
        assert.deepEqual(paths, [['GET', '/json/version']]);
      } else {
        const session = await start;
        assert.equal(session.base, `http://127.0.0.1:${port}`);
        assert.deepEqual(session.page, page);
        await session.activate(page.id);
        assert.deepEqual(paths, [['GET', '/json/version'], ['GET', '/json/list'], ['GET', '/json/activate/own-page']]);
        activationStatus = 404;
        await assert.rejects(() => session.activate(page.id), /could not activate the browser target: http 404 activation body/);
      }
    } finally {
      await close(server, sockets);
      await fs.rm(profile, { recursive: true, force: true });
    }
  });
}

test('D3: a nonanswering version request stops before its guard', async () => {
  const server = net.createServer();
  const sockets = connections(server);
  const profile = await fs.mkdtemp(join(os.tmpdir(), 'devtools-stall-'));
  try {
    const port = await listen(server);
    await fs.writeFile(join(profile, 'DevToolsActivePort'), `${port}\n${browserPath}`);
    const start = startDevToolsSession({
      readActivePort: makeReadActivePort(fs, profile), fetchJson: makeFetchJson(fetch),
      fetchText: makeFetchText(fetch), deadline: makeDeadline(1500),
    });
    // Assert the phase and port, not wall-clock elapsed time (also run with shifted clocks).
    await assert.rejects(() => beforeGuard(start, 4500, 'the JSON request did not stop at its cap'),
      (error) => error.message.includes('/json/version') && error.message.includes(String(port)));
  } finally {
    await close(server, sockets);
    await fs.rm(profile, { recursive: true, force: true });
  }
});

test('D5: the text adapter aborts at its cap before cleanup', async () => {
  const server = net.createServer();
  const sockets = connections(server);
  try {
    const port = await listen(server);
    const request = makeFetchText(fetch)(`http://127.0.0.1:${port}/json/activate/x`, 300);
    await assert.rejects(() => beforeGuard(request, 3000, 'the text request did not stop at its cap'),
      (error) => ['TimeoutError', 'AbortError'].includes(error.name));
  } finally {
    await close(server, sockets);
  }
});

test('P2: remove a complete stale port file before the next browser', async () => {
  const profile = await fs.mkdtemp(join(os.tmpdir(), 'devtools-stale-'));
  try {
    const file = join(profile, 'DevToolsActivePort');
    await fs.writeFile(file, `52568\n${browserPath}`);
    await clearActivePort(fs, profile);
    assert.equal(await makeReadActivePort(fs, profile)(), undefined);
    await clearActivePort(fs, profile); // already absent is harmless
    await assert.rejects(() => clearActivePort({ rm: async () => { throw new Error('read only'); } }, profile),
      /could not remove the stale DevToolsActivePort/);
    await fs.writeFile(file, `52568\n${browserPath}`);
    // A nonexistent executable fails after preparation, without starting a browser.
    const child = spawnSync(process.execPath, [fileURLToPath(new URL('./console2_browser_check.mjs', import.meta.url)),
      join(profile, 'no-browser-executable'), 'about:blank', profile, '[]'], { encoding: 'utf8', timeout: 10000 });
    assert.notEqual(child.status, 0);
    assert.equal(child.error, undefined);
    assert.equal(await makeReadActivePort(fs, profile)(), undefined, 'main must clear the file before spawning');
  } finally {
    await fs.rm(profile, { recursive: true, force: true });
  }
});

test('reader maps only a missing file to not-yet', async () => {
  const missing = Object.assign(new Error('missing'), { code: 'ENOENT' });
  assert.equal(await makeReadActivePort({ readFile: async () => { throw missing; } }, 'profile')(), undefined);
  const denied = Object.assign(new Error('denied'), { code: 'EACCES' });
  await assert.rejects(() => makeReadActivePort({ readFile: async () => { throw denied; } }, 'profile')(),
    (error) => error === denied);
});

run();
