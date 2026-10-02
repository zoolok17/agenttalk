// Shared app harness for the console v2 data/stream tests: a programmable server and a boot
// helper that runs the real scripts against the recording DOM.
// Clock times are drawn in the browser's local zone; pin it so the expected strings hold everywhere.
process.env.TZ = 'UTC';

import { jsonResponse, loadConsole, makeDom, texts, walk } from './console2_harness.mjs';
import { NOW, busyAgents, busyRecent, root } from './console2_fixtures.mjs';

export const HOSTILE = '<img src=x onerror=alert(1)>';
export const LEAD = 'claude-agenttalk-lead';
export const PENDING = { __pending: true };
export const JSON_HANG = { __jsonHang: true };
// P2-b (FIX round 2): a FAILED response whose body never arrives, e.g. JSON_HANG_STATUS(500).
export const JSON_HANG_STATUS = (status) => ({ __jsonHang: true, __status: status });
// #267: the server's busy-scan-bound reply, with a usable retry_after by default.
export const BUSY = (retryAfter = 2) => ({ __status: 503, __body: { error: 'busy', retry_after: retryAfter } });
export const under = (ms) => ms < 5000;   // every timer except the 5 s request timeouts
export const timeouts = (ms) => ms === 5000;

// A programmable server: state/attention/chat handlers may return a payload, a
// {status} problem, or throw; every request is recorded.
export function server(o = {}) {
  const calls = [];
  const clock = { perf: 0 };   // shared with the page: the server's clock moves with it
  const s = {
    calls,
    clock,
    generated: o.generated || (() => new Date(NOW + clock.perf).toISOString()),
    roots: o.roots || (() => [root({ project_id: 'proj-a', agents: busyAgents(), recent: busyRecent(), operator_facing: LEAD })]),
    attention: o.attention || (() => ({ target_root_project_id: 'proj-a', items: [] })),
    chat: o.chat || (() => ({ target_root_project_id: 'proj-a', available: true, lead: LEAD, messages: [] })),
    board: o.board || (() => ({
      schema_version: 1, target_root_project_id: 'proj-a', generated_at: s.generated(),
      coverage: { status: 'complete', valid_until: new Date(NOW + clock.perf + 15000).toISOString() },
      items: [], legacy: { open_request_count: 0, known_lower_bound: 0 }, unassigned: { count: 0 },
      total_count: 0, truncated: false, omitted_count: 0, errors: [], window_days: 7,
    })),
    down: false,
    pendingState: false,   // /api/state never answers
    inits: [],             // the init object of every request, in order
    fetch(url, init) {
      calls.push(url);
      s.inits.push(init);
      if (s.down) return Promise.reject(new Error('down'));
      const u = new URL(url, 'http://x');
      const id = u.searchParams.get('root') || '';
      let payload;
      if (u.pathname === '/api/state' && s.pendingState) return new Promise(() => {});
      if (u.pathname === '/api/state') payload = { schema_version: 1, generated_at: s.generated(), roots: s.roots() };
      else if (u.pathname === '/api/attention') payload = s.attention(id);
      else if (u.pathname === '/api/lead-chat') payload = s.chat(id);
      else if (u.pathname === '/api/work-board') payload = s.board(id);
      else return jsonResponse({}, 404);
      if (payload && payload.__pending) return new Promise(() => {});                       // never settles
      if (payload && payload.__jsonHang) {
        // P2-b (FIX round 2): an OPTIONAL __status lets this simulate a FAILED response whose
        // body never arrives (a 500 that hangs), distinct from JSON_HANG's own 200-that-hangs -
        // only a caller that actually reads error bodies (the attention fetch, 503 only) can
        // ever be held up by this one.
        const status = payload.__status || 200;
        return Promise.resolve({ ok: status >= 200 && status < 300, status, json: () => new Promise(() => {}) });
      }
      if (payload && payload.__status) return jsonResponse(payload.__body || {}, payload.__status);
      return jsonResponse(payload);
    },
  };
  return s;
}

export async function boot(srv, opts = {}) {
  const dom = makeDom();
  const loaded = loadConsole({ dom, fetch: (u, i) => srv.fetch(u, i), clock: srv.clock, ...opts });
  for (let i = 0; i < 12; i++) await new Promise((r) => setTimeout(r, 0));
  return { dom, ...loaded };
}

export const stream = (dom) => dom.document.getElementById('c2-stream');
export const rail = (dom) => dom.document.getElementById('c2-rail');
export const board = (dom) => dom.document.getElementById('c2-board');
export const boardDetail = (dom) => dom.document.getElementById('c2-board-detail');
export const boardCadence = (ms) => ms === 5000;
export const all = (node) => texts(node).join(' | ');
export const app = (dom) => dom.document.getElementById('app');
export const header = (dom) => dom.document.getElementById('c2-header');
export const classOf = (node, cls) => walk(node).filter((n) => (n.className || '').split(' ').includes(cls));
export const chips = (dom) => walk(header(dom)).filter((n) => n.getAttribute && n.getAttribute('data-c2-key') !== null);

