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
      else return jsonResponse({}, 404);
      if (payload && payload.__pending) return new Promise(() => {});                       // never settles
      if (payload && payload.__jsonHang) return Promise.resolve({ ok: true, status: 200, json: () => new Promise(() => {}) });
      if (payload && payload.__status) return jsonResponse({}, payload.__status);
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
export const all = (node) => texts(node).join(' | ');
export const app = (dom) => dom.document.getElementById('app');
export const header = (dom) => dom.document.getElementById('c2-header');
export const classOf = (node, cls) => walk(node).filter((n) => (n.className || '').split(' ').includes(cls));
export const chips = (dom) => walk(header(dom)).filter((n) => n.getAttribute && n.getAttribute('data-c2-key') !== null);

