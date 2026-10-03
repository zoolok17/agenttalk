// Feeds a REAL server payload (an agent row and the attention items, exported by the Python test
// that built them) through the console 2 view model and prints what the page would show.
// Usage: node tests/console2_real_payload.mjs <payload.json>
import fs from 'node:fs';
import { createRequire } from 'node:module';
import { CONN_OK, NOW, agent, attention, root } from './console2_fixtures.mjs';

const M = createRequire(import.meta.url)('../src/agenttalk/web_static/console2-model.js');
const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const row = { ...agent(payload.row.name), ...payload.row };
const team = M.buildTeamView({
  nowMs: NOW, generatedMs: NOW, tz: 'utc', conn: CONN_OK, ui: {}, canAct: false,
  root: root({ agents: [row] }), attention: attention(payload.items), chat: null,
});
const view = M.agentView(row, { nowMs: NOW, generatedMs: NOW, recent: [], teamIds: [], project: 'x', known: [], tz: 'utc' });
console.log(JSON.stringify({
  state: view.state, line: view.line,
  cards: team.needs.open.map((c) => ({ kind: c.kind, evidence: c.evidence })),
}));
