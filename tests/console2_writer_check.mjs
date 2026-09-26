// Reads {scenarios: [{name, evalMs, agent}]} from a JSON file, runs each agent row through
// the console v2 model (agentView) and prints {name: {state, line, stuck, candidate}}.
// The rows are produced by tests/test_console2_health_writer.py from the REAL
// WrapperHealthWriter and Store.read_health, not hand-written health JSON.
import fs from 'node:fs';
import { createRequire } from 'node:module';

const M = createRequire(import.meta.url)('../src/agenttalk/web_static/console2-model.js');
const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = {};
for (const sc of input.scenarios) {
  const v = M.agentView(sc.agent, {
    nowMs: sc.evalMs, generatedMs: sc.evalMs, recent: sc.recent || [], teamIds: [sc.agent.name],
    project: 'probe', known: ['probe'], tz: 'utc',
  });
  out[sc.name] = { state: v.state, line: v.line, stuck: v.stuck ? v.stuck.evidence : null, candidate: v.candidate };
}
process.stdout.write(JSON.stringify(out));
