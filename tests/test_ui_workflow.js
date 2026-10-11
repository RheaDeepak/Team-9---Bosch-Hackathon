// Lifecycle stepper + Agent & Workflow Activity models (the wf:pure block of static/index.html) and the shared renderers.
// Run: node tests/test_ui_workflow.js   (also run by tests/test_ui_comparison.py)
const fs = require('fs'), path = require('path'), assert = require('assert');
const ChaosUI = require(path.join(__dirname, '..', 'static', 'chaos-ui.js'));
const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8').replace(/\r\n/g, '\n');
const block = html.match(/\/\* wf:pure begin[\s\S]*?\*\/\n([\s\S]*?)\/\* wf:pure end \*\//)[1];
const { lifecycleModel, agentModel } = new Function('ChaosUI', block + '; return { lifecycleModel, agentModel };')(ChaosUI);

const META = ['topology', 'planner', 'policy', 'executor', 'guard', 'analysis'].map(id => ({ id, name: id.toUpperCase(), summary: 's', module: id + '.py', implementation: 'implemented', notes: [] }));
const plan = { fault: 'network_delay', target: 'quote', magnitude: 200, duration_s: 60 };
const okVal = { valid: true, reasons: [], checks: [{ name: 'a', passed: true }, { name: 'b', passed: true }] };
const base = (over = {}) => ({ phase: 'idle', running: false, plan: null, validation: null, source: null, note: null, samples: [], events: [], result: null, refused: null, ...over });
const graph = { state: 'ok', nodes: 21, edges: 32, protected: 5 };
const model = s => agentModel({ status: s, meta: META, graph, goal: '', agent: null });
const states = m => Object.fromEntries(m.steps.map(x => [x.id, x.state]));
const rows = s => Object.fromEntries(model(s).map(r => [r.id, r]));
const result = (over = {}) => ({ guard: { aborted: false, reason: 'duration elapsed (60s)' }, rollback_verified: true, recovery: { recovered: true, seconds: 5 },
  score: { resilience_score: 80 }, verdict: { verdict: 'held' }, ...over });

const tests = {
  'idle: every lifecycle step pending, every component idle, topology shows what was discovered'() {
    const m = lifecycleModel(base());
    assert.ok(m.steps.every(x => x.state === 'pending'));
    const r = rows(base());
    assert.deepStrictEqual(['planner', 'policy', 'executor', 'guard', 'analysis'].map(k => r[k].runtime), Array(5).fill('idle'));
    assert.strictEqual(r.topology.runtime, 'completed');
    assert.strictEqual(r.topology.latest, '21 services, 32 dependencies discovered, 5 protected');
  },
  'topology: loading and failed states come from the real graph request'() {
    assert.strictEqual(agentModel({ status: base(), meta: META, graph: { state: 'loading' } })[0].runtime, 'loading');
    const f = agentModel({ status: base(), meta: META, graph: { state: 'error', error: 'HTTP 500' } })[0];
    assert.deepStrictEqual([f.runtime, f.latest], ['failed', 'Graph unavailable: HTTP 500']);
  },
  'valid proposal: human approval is the current step and the executor is waiting, not running'() {
    const s = base({ phase: 'proposed', plan, validation: okVal, source: 'rank_agent', note: 'model x, 8 tool calls' });
    const m = lifecycleModel(s), st = states(m), r = rows(s);
    assert.deepStrictEqual([st.proposed, st.validated, st.approved, st.injecting], ['done', 'done', 'current', 'pending']);
    assert.ok(m.steps.find(x => x.id === 'approved').checkpoint);
    assert.match(m.headline, /human approval; nothing has been injected/);
    assert.strictEqual(r.executor.runtime, 'waiting');
    assert.match(r.executor.latest, /Approval required before injecting network delay on quote/);
    assert.deepStrictEqual([r.policy.runtime, r.policy.latest], ['completed', '2 of 2 checks passed']);
    assert.match(r.planner.latest, /planning agent \(an LLM investigated the top candidates\).*model x, 8 tool calls/);
  },
  'policy rejection: validated fails, nothing can run, the executor is blocked'() {
    const s = base({ phase: 'proposed', plan, validation: { valid: false, reasons: ['target is protected'], checks: [] } });
    const st = states(lifecycleModel(s)), r = rows(s);
    assert.deepStrictEqual([st.proposed, st.validated, st.approved], ['done', 'failed', 'pending']);
    assert.deepStrictEqual([r.policy.runtime, r.executor.runtime], ['blocked', 'blocked']);
    assert.match(r.policy.latest, /target is protected/);
  },
  'proposing: planner running with the agent counters, or the single LLM call when a goal is chosen'() {
    const s = base({ phase: 'proposing' });
    const a = agentModel({ status: s, meta: META, graph, goal: '', agent: { llm: 2, tools: 3, cap: 8 } }).find(x => x.id === 'planner');
    assert.deepStrictEqual([a.runtime, a.latest], ['running', 'Agent running: 2 LLM calls, 3 of 8 tool calls so far']);
    assert.match(agentModel({ status: s, meta: META, graph, goal: 'recovery_time', agent: null }).find(x => x.id === 'planner').latest, /Making an LLM call/);
    assert.strictEqual(states(lifecycleModel(s)).proposed, 'current');
  },
  'monitoring: injection done, observing current, guard running and reporting real breaches'() {
    const s = base({ phase: 'monitoring', running: true, plan, validation: okVal, samples: [{ breaches: {} }, { breaches: { 'frontend p95_ms': '3006ms > 133ms' } }] });
    const st = states(lifecycleModel(s)), r = rows(s);
    assert.deepStrictEqual([st.approved, st.injecting, st.monitoring, st.rolling_back], ['done', 'done', 'current', 'pending']);
    assert.deepStrictEqual([r.executor.runtime, r.guard.runtime], ['running', 'running']);
    assert.match(r.guard.latest, /2 sample\(s\); breach: frontend p95_ms 3006ms > 133ms/);
    assert.match(rows(base({ phase: 'monitoring', plan, samples: [{ breaches: {} }] })).guard.latest, /no breach so far/);
  },
  'completed run: every step done; executor, guard and analysis completed with real results'() {
    const s = base({ phase: 'done', plan, validation: okVal, result: result() });
    const m = lifecycleModel(s), r = rows(s);
    assert.ok(m.steps.every(x => x.state === 'done'));
    assert.strictEqual(m.outcome, 'completed');
    assert.deepStrictEqual([r.executor.runtime, r.guard.runtime, r.analysis.runtime], ['completed', 'completed', 'completed']);
    assert.match(r.guard.latest, /Recovery verified after 5 s/);
    assert.strictEqual(r.analysis.latest, 'Resilience 80/100, verdict: held');
  },
  'aborted run never looks successful: the last step is Aborted, rollback and recovery stay visible'() {
    const s = base({ phase: 'done', plan, result: result({ guard: { aborted: true, reason: 'threshold breached 2 samples in a row: frontend p95_ms' } }) });
    const m = lifecycleModel(s), st = states(m), last = m.steps[m.steps.length - 1];
    assert.deepStrictEqual([last.label, last.state, m.outcome], ['Aborted', 'aborted', 'aborted']);
    assert.deepStrictEqual([st.rolling_back, st.recovering, st.analyzing], ['done', 'done', 'done']);
    assert.match(m.headline, /^Aborted and rolled back: threshold breached/);
    assert.match(rows(s).guard.latest, /^Aborted the fault: threshold breached/);
    const stopped = lifecycleModel(base({ phase: 'done', plan, result: result({ guard: { aborted: true, reason: 'stopped by user (STOP button)' } }) }));
    assert.strictEqual(stopped.steps[stopped.steps.length - 1].note, 'stopped by operator');
  },
  'unverified rollback or recovery is shown as failed, never as completed'() {
    const m = lifecycleModel(base({ phase: 'done', plan, result: result({ recovery: { recovered: false } }) }));
    assert.deepStrictEqual([states(m).recovering, m.steps[m.steps.length - 1].label, m.outcome], ['failed', 'Needs attention', 'failed']);
    const bad = base({ phase: 'done', plan, result: result({ rollback_verified: false }) });
    assert.strictEqual(states(lifecycleModel(bad)).rolling_back, 'failed');
    assert.deepStrictEqual([rows(bad).executor.runtime, rows(bad).executor.latest.includes('ROLLBACK NOT VERIFIED')], ['failed', true]);
  },
  'refused and crashed runs stop at the right step and say why'() {
    const refused = base({ phase: 'refused', plan, refused: 'no baseline.json' });
    assert.strictEqual(states(lifecycleModel(refused)).injecting, 'failed');
    assert.deepStrictEqual([rows(refused).executor.runtime, rows(refused).executor.latest], ['blocked', 'Not injected: no baseline.json']);
    assert.strictEqual(states(lifecycleModel(base({ phase: 'error', fault_started_ts: 1, refused: 'boom' }))).monitoring, 'failed');
  },
  'implementation maturity is separate from runtime status'() {
    const meta = META.map(m => m.id === 'executor' ? { ...m, implementation: 'planned', notes: [{ text: 'Packet-loss fault', status: 'planned' }] } : m);
    const run = agentModel({ status: base({ phase: 'monitoring', plan, samples: [] }), meta, graph }).find(x => x.id === 'executor');
    assert.deepStrictEqual([run.maturity, run.runtime], ['planned', 'running']);
    const idle = agentModel({ status: base(), meta, graph }).find(x => x.id === 'planner');
    assert.deepStrictEqual([idle.maturity, idle.runtime], ['implemented', 'idle']);          // implemented but idle: not "unfinished"
    assert.strictEqual(agentModel({ status: base(), meta: [], graph })[0].maturity, 'unknown'); // no metadata: say so, do not guess
    assert.strictEqual(agentModel({ status: base(), meta: [], graph })[0].name, 'Topology Discovery');
  },
  'renderers: both chips are shown, text is escaped, the current step is announced, states are words as well as colours'() {
    const row = { id: 'executor', name: '<b>x</b>', summary: 's', maturity: 'in_development', runtime: 'waiting', latest: '<img src=x>', notes: [{ text: 'Packet-loss fault', status: 'planned' }] };
    const h = ChaosUI.agentRowHtml(row);
    assert.ok(h.includes('Waiting for approval') && h.includes('In development') && h.includes('Packet-loss fault'));
    assert.ok(!h.includes('<b>x</b>') && !h.includes('<img'));
    const steps = ChaosUI.stepperHtml(lifecycleModel(base({ phase: 'proposed', plan, validation: okVal })));
    assert.match(steps, /aria-current="step"/);
    assert.match(steps, /Proposed<span class="sr"> \(completed\)/);
    assert.match(steps, /human checkpoint/);
    assert.match(ChaosUI.stepperHtml({ steps: ChaosUI.buildSteps([{ id: 'a', label: 'A' }], 'a', 'failed') }), /\(failed\)/);
  },
  'mmss formats real seconds and stays empty for no timer'() {
    assert.deepStrictEqual([ChaosUI.mmss(75), ChaosUI.mmss(5), ChaosUI.mmss(null)], ['1:15', '0:05', '']);
  },
};
let failed = 0;
for (const [name, fn] of Object.entries(tests)) { try { fn(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + e.message); } }
console.log(failed ? `${failed} failed` : 'all passed');
process.exit(failed ? 1 : 0);
