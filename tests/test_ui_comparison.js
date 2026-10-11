// Unit tests for the pure helpers behind the "Normal vs Under attack" charts (the cmp:pure block of static/index.html).
// Run: node tests/test_ui_comparison.js   (also run by tests/test_ui_comparison.py when node is installed)
const fs = require('fs'), assert = require('assert');
const html = fs.readFileSync(require('path').join(__dirname, '..', 'static', 'index.html'), 'utf8').replace(/\r\n/g, '\n');
const block = html.match(/\/\* cmp:pure begin[\s\S]*?\*\/\n([\s\S]*?)\/\* cmp:pure end \*\//)[1];
const api = new Function(block + '; return { CMP_METRICS, niceCeil, medianOf, cmpSpan, normalSeries, attackSeries, affectedScore, pickServices, yRange, summaryLine, markersFor };')();
const { CMP_METRICS, niceCeil, cmpSpan, normalSeries, attackSeries, pickServices, yRange, summaryLine, markersFor } = api;
const [P95, ERR] = CMP_METRICS;

const row = (t, v) => ({ t, services: Object.fromEntries(Object.entries(v).map(([s, [p95, err]]) => [s, { p95_ms: p95, error_rate: err }])) });
const data = (over = {}) => ({
  plan: { target: 'cart', fault: 'network_delay', duration_s: 60 },
  services: ['cart', 'frontend', 'checkout', 'shipping', 'email'],
  normal: [-120, -90, -60, -30, -5, 0].map(t => row(t, { cart: [20, 0], frontend: [34, 0], checkout: [30, 0], shipping: [10, 0], email: [8, 0] })),
  attack: [0, 20, 40, 60].map((t, i) => row(t, { cart: [20 + 500 * i, 0], frontend: [34 + 160 * i, 0.5 * i], checkout: [30 + 50 * i, 0], shipping: [10, 0], email: [8 + i, 0] })),
  baseline: { cart: { p95_ms: 20, error_rate: 0 }, frontend: { p95_ms: 34, error_rate: 0 } },
  rollback_at_s: 60, aborted: false, stopped: false, ...over });

const tests = {
  'nice ceilings are round numbers at or above the value'() {
    assert.deepStrictEqual([0.3, 7, 12, 505, 1999, 0].map(niceCeil), [0.3, 8, 15, 600, 2000, 1]);
  },
  'both sides share one span: the planned duration, or the longest attack sample'() {
    assert.strictEqual(cmpSpan(data()), 60);
    assert.strictEqual(cmpSpan(data({ attack: [row(0, {}), row(83, {})] })), 85);
    assert.strictEqual(cmpSpan(data({ plan: { target: 'cart', duration_s: 5 }, attack: [] })), 20);
    assert.strictEqual(cmpSpan(data({ plan: { target: 'cart', duration_s: 900 } })), 180);
  },
  'the normal side is the last span seconds before injection, ending at x = span'() {
    const pts = normalSeries(data(), 'frontend', 'p95_ms').points;
    assert.deepStrictEqual(pts.map(p => p.x), [0, 30, 55, 60]);          // -60, -30, -5, 0 shifted by the span; older points are cut
    assert.ok(!normalSeries(data(), 'frontend', 'p95_ms').fallback);
  },
  'without pre-fault history the normal side falls back to the baseline average, flagged'() {
    const s = normalSeries(data({ normal: null }), 'frontend', 'p95_ms');
    assert.deepStrictEqual([s.fallback, s.points], [true, [{ x: 0, y: 34 }, { x: 60, y: 34 }]]);
    assert.deepStrictEqual(normalSeries(data({ normal: null }), 'shipping', 'p95_ms').points, []);   // nothing known: empty, not invented
    const empty = data({ normal: [-5, 0].map(t => row(t, { frontend: [null, null] })) });             // history with no usable numbers
    assert.ok(normalSeries(empty, 'frontend', 'p95_ms').fallback);
  },
  'default selection is the target plus the 3 most affected services; show-all lists every service'() {
    assert.deepStrictEqual(pickServices(data(), false), ['cart', 'frontend', 'checkout', 'email']);
    assert.deepStrictEqual(pickServices(data(), true), data().services);
    assert.deepStrictEqual(pickServices(data({ services: ['frontend'] }), false), ['frontend']);       // a target that is not watched is not invented
  },
  'a row has ONE y range, computed from the larger side'() {
    const d = data(), svcs = pickServices(d, false);
    const r = yRange(d, svcs, P95, false);
    assert.deepStrictEqual([r.min, r.max], [0, 2000]);                    // attack peak 1520 dwarfs normal 34: both charts get 2000
    const calm = data({ attack: [row(0, { cart: [21, 0] })] });
    assert.strictEqual(yRange(calm, ['cart'], P95, false).max, 25);       // calm attack: the normal side (34 for frontend is hidden) sets it
    const e = yRange(d, svcs, ERR, false);
    assert.deepStrictEqual([e.min, e.max], [0, 2]);         // peak 1.5 % -> 2
  },
  'log scale keeps a positive floor so zero error rates stay drawable'() {
    const r = yRange(data(), ['cart', 'frontend'], ERR, true);
    assert.strictEqual(r.min, 0.1);
    assert.ok(r.max >= 1);
    assert.strictEqual(yRange(data(), ['cart'], P95, true).min, 1);
  },
  'summary names the most affected service with before and after'() {
    assert.strictEqual(summaryLine(data(), P95), 'cart: 20 ms → 1520 ms (76× slower)');
    assert.strictEqual(summaryLine(data(), ERR), 'frontend: 0.0% → 1.5% (+1.5 points)');
    const calm = data({ attack: [row(0, { cart: [20, 0], frontend: [34, 0] })] });
    assert.ok(summaryLine(calm, P95).endsWith('(no change)'));
    assert.ok(summaryLine(data({ attack: [] }), P95).startsWith('No p95 latency data'));
    const mild = data({ attack: [row(0, { frontend: [34, 0], cart: [20, 0] }), row(20, { frontend: [51, 0], cart: [20, 0] })] });
    assert.strictEqual(summaryLine(mild, P95), 'frontend: 34 ms → 51 ms (1.5× slower)');
  },
  'summary uses the baseline when there is no history (past results)'() {
    const d = data({ normal: null, attack: [row(0, { cart: [20, 0], frontend: [340, 0] })] });
    assert.strictEqual(summaryLine(d, P95), 'frontend: 34 ms → 340 ms (10× slower)');
  },
  'markers: injection, then rollback, auto-abort or stop'() {
    assert.deepStrictEqual(markersFor(data()).map(m => m.label), ['fault injected', 'rollback']);
    assert.strictEqual(markersFor(data({ aborted: true }))[1].label, 'auto-abort + rollback');
    assert.strictEqual(markersFor(data({ aborted: true, stopped: true }))[1].label, 'stop + rollback');
    assert.strictEqual(markersFor(data({ rollback_at_s: null })).length, 1);
  },
  'series keep services aligned: same keys on both sides'() {
    const d = data();
    for (const svc of d.services) assert.ok(normalSeries(d, svc, 'p95_ms').points.length && attackSeries(d, svc, 'p95_ms').length);
  },
};
let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { fn(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + e.message); }
}
console.log(failed ? `${failed} failed` : 'all passed');
process.exit(failed ? 1 : 0);
