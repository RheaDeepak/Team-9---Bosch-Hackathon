// The "planning agent is running, making LLM calls" message (agentStats / agentRunningHtml in static/index.html).
const fs = require('fs'), assert = require('assert');
const html = fs.readFileSync(require('path').join(__dirname, '..', 'static', 'index.html'), 'utf8').replace(/\r\n/g, '\n');
const src = html.match(/const agentStats = [\s\S]*?const AGENT_TOOL_CAP = 8;/)[0];
const make = ignoreRun => new Function('mainFeed', 'esc', src + '; return { agentStats, noteAgentEvents, agentRunningHtml };')({ ignoreRun }, s => String(s));
let n = 0;
const ev = (run, type, summary = '', extra = {}) => ({ run_id: run, type, summary, step: ++n, ...extra });
const text = h => h.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');

const tests = {
  'before any event: says LLM calls are being made and that the first one is about to start'() {
    const t = text(make('').agentRunningHtml());
    assert.match(t, /planning agent is running and making LLM calls/);
    assert.match(t, /about to make its first LLM call/);
    assert.match(t, /LLM calls so far: 0, tool calls: 0 of 8/);
  },
  'after a tool result the next step is an LLM call, and the counters follow the events'() {
    const m = make('');
    m.noteAgentEvents([ev('r1', 'thought', 'investigating top-3'), ev('r1', 'llm', 'answered by gemini-3.5-flash-lite'),
                       ev('r1', 'tool_call', 'get_edges(...)'), ev('r1', 'tool_result', '{}')]);
    const t = text(m.agentRunningHtml());
    assert.match(t, /LLM call in progress: waiting for the model/);
    assert.match(t, /LLM calls so far: 1, tool calls: 1 of 8/);
    m.noteAgentEvents([ev('r1', 'llm', 'answered by gemini-3.5-flash-lite')]);
    assert.match(text(m.agentRunningHtml()), /the model answered; running its read-only tool calls/);
    assert.match(text(m.agentRunningHtml()), /LLM calls so far: 2/);
  },
  'LLM retry notes are not counted as calls; analysis events are ignored'() {
    const m = make('');
    m.noteAgentEvents([ev('r1', 'llm', 'gemini-3.5-flash-lite: HTTP 429, waiting 13s (retry 1/2)'), ev('r2', 'tool_call', 'x', { mode: 'analysis' })]);
    assert.deepStrictEqual([m.agentStats.llm, m.agentStats.tools], [0, 0]);
  },
  'events delivered twice (a feed reset re-reads them) are counted once'() {
    const m = make(''), batch = [ev('r1', 'llm', 'answered by x', { step: 1 }), ev('r1', 'tool_call', 'a', { step: 2 })];
    m.noteAgentEvents(batch); m.noteAgentEvents(batch);
    assert.deepStrictEqual([m.agentStats.llm, m.agentStats.tools], [1, 1]);
  },
  'a new run resets the counters; the previous session is not shown as progress'() {
    const m = make('old');
    m.noteAgentEvents([ev('old', 'llm', 'answered by x'), ev('old', 'tool_call', 'a')]);
    assert.match(text(m.agentRunningHtml()), /about to make its first LLM call.*LLM calls so far: 0, tool calls: 0/);   // 'old' is the ignored session
    m.noteAgentEvents([ev('new', 'llm', 'answered by x')]);
    assert.deepStrictEqual([m.agentStats.run, m.agentStats.llm, m.agentStats.tools], ['new', 1, 0]);
    assert.match(text(m.agentRunningHtml()), /LLM calls so far: 1/);
  },
};
let failed = 0;
for (const [name, fn] of Object.entries(tests)) { try { fn(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + e.message); } }
console.log(failed ? `${failed} failed` : 'all passed');
process.exit(failed ? 1 : 0);
