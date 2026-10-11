/* Chaos Copilot shared UI helpers, loaded by BOTH UIs (static/ and frontend/). Keep the two copies identical
   (tests/test_design_system.py). Pure rendering only: no fetches, no state machine. Each UI maps its own backend
   state onto the small models below; nothing here invents progress, events or timestamps.

   Agent row model:  { id, name, summary, module?, maturity: 'implemented'|'in_development'|'planned',
                       runtime: 'idle'|'loading'|'running'|'waiting'|'completed'|'blocked'|'failed'|'unavailable',
                       latest?: string, notes?: [{ text, status }] }
   Stepper model:    { steps: [{ id, label, state: 'done'|'current'|'pending'|'failed'|'aborted', checkpoint?: bool, note?: string }] } */
(function (root) {
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const P = {                                                  // 24x24 stroke icons (inline SVG, no icon font)
    topology: '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M8 7l3 9M16 7l-3 9M8.5 6h7"/>',
    planner: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>',
    policy: '<path d="M12 3l7 3v5c0 5-3 8.5-7 10-4-1.5-7-5-7-10V6z"/><path d="M9 12l2 2 4-4"/>',
    executor: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
    guard: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    analysis: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>', x: '<path d="M6 6l12 12M18 6L6 18"/>',
    idle: '<circle cx="12" cy="12" r="8"/>', spinner: '<path d="M12 3a9 9 0 019 9" />',
    hand: '<path d="M8 12V5a1.5 1.5 0 013 0v6M11 11V4a1.5 1.5 0 013 0v7M14 11V6a1.5 1.5 0 013 0v8c0 4-2 7-6 7-3 0-4.5-1.5-6-4l-1.5-3a1.5 1.5 0 012.5-1.5L8 14"/>',
    alert: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18h.01"/>', stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    user: '<circle cx="12" cy="8" r="3.5"/><path d="M5 21c0-4 3-6 7-6s7 2 7 6"/>', plug: '<path d="M9 2v6M15 2v6M6 8h12v4a6 6 0 01-12 0zM12 18v4"/>',
    wrench: '<path d="M14.5 6.5a4 4 0 005 5L21 13l-8 8-4-4 8-8z"/>', list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  };
  const icon = (name, cls = '') => `<svg class="cc-svg ${cls}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${P[name] || P.idle}</svg>`;
  const chip = (kind, label, ic) => `<span class="cc-chip ${kind}">${ic ? icon(ic, ic === 'spinner' ? 'cc-spin' : '') : ''}${esc(label)}</span>`;

  const MATURITY = { implemented: ['outline', 'Implemented', 'check'], in_development: ['warn', 'In development', 'wrench'], planned: ['dashed', 'Planned', 'list'],
                     unknown: ['dashed', 'Maturity unknown', 'idle'] };
  const RUNTIME = { idle: ['', 'Idle', 'idle'], loading: ['info', 'Loading', 'spinner'], running: ['info', 'Running', 'spinner'], waiting: ['warn', 'Waiting for approval', 'hand'],
                    completed: ['ok', 'Completed', 'check'], blocked: ['bad', 'Blocked', 'alert'], failed: ['bad', 'Failed', 'x'], unavailable: ['dashed', 'Not connected', 'plug'] };
  const maturityChip = m => { const [k, l, i] = MATURITY[m] || MATURITY.unknown; return chip(k, l, i); };
  const runtimeChip = r => { const [k, l, i] = RUNTIME[r] || RUNTIME.idle; return chip(k, l, i); };

  function agentRowHtml(a) {
    const rt = RUNTIME[a.runtime] ? a.runtime : 'idle';
    const notes = (a.notes || []).map(n => `<li>${chip(n.status === 'planned' ? 'dashed' : n.status === 'in_development' ? 'warn' : 'outline', n.text, n.status === 'planned' ? 'list' : n.status === 'in_development' ? 'wrench' : 'check')}</li>`).join('');
    return `<li class="cc-agent ${rt}" data-agent="${esc(a.id)}"><span class="cc-agent-ic">${icon(a.id)}</span>
      <div><h4>${esc(a.name)}${a.module ? ` <code>${esc(a.module)}</code>` : ''}</h4><div class="cc-agent-sum">${esc(a.summary)}</div></div>
      <div class="cc-agent-state">${runtimeChip(rt)}${maturityChip(a.maturity)}</div>
      <div class="cc-agent-latest">${esc(a.latest || '')}</div>${notes ? `<ul class="cc-agent-notes" aria-label="Capabilities of ${esc(a.name)} still being built">${notes}</ul>` : ''}</li>`;
  }
  const agentsHtml = rows => `<ul class="cc-agents" aria-label="Agents and workflow components">${rows.map(agentRowHtml).join('')}</ul>`;

  const STEP_WORD = { done: 'completed', current: 'in progress', pending: 'not reached', failed: 'failed', aborted: 'aborted' };
  function stepHtml(s, i) {
    const inner = s.state === 'done' ? icon('check') : s.state === 'failed' ? icon('x') : s.state === 'aborted' ? icon('stop') : s.checkpoint ? icon('user') : String(i + 1);
    return `<li class="cc-step ${s.state}${s.checkpoint ? ' checkpoint' : ''}"${s.state === 'current' ? ' aria-current="step"' : ''}>
      <span class="cc-dot" aria-hidden="true">${inner}</span><span>${esc(s.label)}${s.checkpoint ? ' <span class="cc-step-note">human checkpoint</span>' : ''}<span class="sr"> (${STEP_WORD[s.state] || s.state})</span>${s.note ? `<span class="cc-step-note" style="display:block">${esc(s.note)}</span>` : ''}</span></li>`;
  }
  const stepperHtml = model => `<ol class="cc-steps" aria-label="Experiment lifecycle">${model.steps.map(stepHtml).join('')}</ol>`;

  /* Generic stepper builder: `order` is the backend's own ordered phases; `at` is the current phase id (or null);
     `outcome` is 'completed' | 'aborted' | 'failed' | null. Phases before `at` are done, `at` is current, later ones are pending.
     A failed/aborted outcome marks the phase where it ended (`at`) and keeps everything later pending: never green. */
  function buildSteps(order, at, outcome) {
    const idx = order.findIndex(s => s.id === at);
    return order.map((s, i) => {
      let state = 'pending';
      if (outcome === 'completed') state = 'done';
      else if (idx >= 0 && i < idx) state = 'done';
      else if (idx >= 0 && i === idx) state = outcome === 'failed' ? 'failed' : outcome === 'aborted' ? 'aborted' : 'current';
      return { ...s, state };
    });
  }
  const mmss = sec => sec == null || !isFinite(sec) ? '' : `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, '0')}`;

  const api = { esc, icon, chip, maturityChip, runtimeChip, agentRowHtml, agentsHtml, stepperHtml, buildSteps, mmss, MATURITY, RUNTIME };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.ChaosUI = api;
})(typeof window !== 'undefined' ? window : globalThis);
