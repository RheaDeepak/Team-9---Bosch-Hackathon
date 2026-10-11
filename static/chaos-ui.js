/* Chaos Copilot shared UI helpers, loaded by BOTH UIs (static/ and frontend/). Keep the two copies identical
   (tests/test_design_system.py). Pure rendering only: no fetches, no state machine. Each UI maps its own backend
   state onto the small models below; nothing here invents progress, events or timestamps.

   Agent row model:  { id, name, summary, module?, maturity: 'implemented'|'in_development'|'planned',
                       runtime: 'idle'|'loading'|'running'|'waiting'|'completed'|'blocked'|'failed'|'unavailable',
                       latest?: string, notes?: [{ text, status }] }
   Lifecycle steps:  [{ id, label, state: 'done'|'current'|'pending'|'failed'|'aborted', checkpoint?: bool, note?: string }] (shown by the dock) */
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

  const STEP_WORD = { done: 'completed', current: 'in progress', pending: 'not reached', failed: 'failed', aborted: 'aborted' };
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


  /* ---- Activity dock: the one place that shows the lifecycle and the six components. A compact floating dashboard (fixed top right;
     a slim bar at the bottom on narrow screens). Pure part first (dockOpen, dockParts: unit-tested), then the small DOM controller.
     Dock model: { active, running, stage, headline, elapsed, steps, faultLabel, fault, events: [{ time, text }] (newest first),
                   rows: [{ id, name, runtime, latest, summary, module, maturity, notes }] }.
     STOP is only ever shown here and never has logic of its own: onStop() is the page's single STOP handler. */
  const DOCK_ROWS_LATEST = 90;
  const DOCK_CAPTION = 'Deterministic policy, not the LLM, decides whether an experiment may run, and a human approves it before anything is injected.';
  // open state: an explicit choice for this session wins; otherwise open while an experiment is active, but never auto-open on narrow screens
  const dockOpen = (pref, active, narrow) => pref === 'open' ? true : pref === 'closed' ? false : narrow ? false : !!active;
  const clip = (t, n) => { const x = String(t || ''); return x.length > n ? x.slice(0, n - 1).trimEnd() + '…' : x; };
  const noteChip = n => chip(n.status === 'planned' ? 'dashed' : n.status === 'in_development' ? 'warn' : 'outline', n.text, n.status === 'planned' ? 'list' : n.status === 'in_development' ? 'wrench' : 'check');
  function dockParts(m) {
    const stage = `${esc(m.stage || 'Idle')}${m.elapsed ? ` · <span class="cc-num">${esc(m.elapsed)}</span>` : ''}`;
    const steps = m.steps || [], at = steps.findIndex(x => x.state === 'current' || x.state === 'failed' || x.state === 'aborted');
    const cur = at >= 0 ? at : steps.length && steps.every(x => x.state === 'done') ? steps.length - 1 : -1;      // -1: nothing has started
    const dots = steps.map((x, k) => `<li class="cc-mdot ${x.state}" title="${k + 1} · ${esc(x.label)} (${STEP_WORD[x.state] || x.state})"><span class="sr">${esc(x.label)}: ${STEP_WORD[x.state] || x.state}</span></li>`).join('');
    const rows = (m.rows || []).map(r => {
      const notes = (r.notes || []).map(n => `<li>${noteChip(n)}</li>`).join('');
      return `<li class="cc-drow ${esc(r.runtime)}" data-agent="${esc(r.id)}"><div class="cc-drow-top" role="button" tabindex="0" aria-expanded="false"><span class="cc-agent-ic">${icon(r.id)}</span><span class="cc-drow-name">${esc(r.name)}</span>${runtimeChip(r.runtime)}</div>${r.latest
        ? `<div class="cc-drow-latest" title="${esc(r.latest)}" data-full="${esc(r.latest)}" data-short="${esc(clip(r.latest, DOCK_ROWS_LATEST))}">${esc(r.latest)}</div>` : ''}`
        + `<div class="cc-drow-detail"><p>${esc(r.summary || 'No description available.')}</p><div class="cc-drow-meta">${r.module ? `<code>${esc(r.module)}</code>` : ''}${maturityChip(r.maturity)}</div>`
        + `${notes ? `<ul class="cc-drow-notes" aria-label="Capabilities of ${esc(r.name)} still being built">${notes}</ul>` : ''}</div></li>`;
    }).join('');
    const events = (m.events || []).slice(0, 3).map(e => `<li><span class="cc-ev-time cc-num">${esc(e.time)}</span><span class="cc-ev-text" title="${esc(e.text)}">${esc(clip(e.text, 120))}</span></li>`).join('');
    return {
      stage, dots, rows, events, headline: m.headline || '',
      stepText: steps.length && cur >= 0 ? `${cur + 1} · ${esc(steps[cur].label)}` : 'Not started',
      dotsLabel: steps.length ? `Lifecycle: ${cur >= 0 ? steps[cur].label : 'not started'}, step ${cur >= 0 ? cur + 1 : 0} of ${steps.length}` : 'Lifecycle',
      fault: m.fault ? `<span class="cc-label">${esc(m.faultLabel || 'Fault')}</span><b>${esc(m.fault)}</b>` : '<span class="cc-label">Fault</span><span class="muted">none</span>',
    };
  }
  const DOCK_SKELETON = `<div class="cc-dock-head">
      <button type="button" class="cc-dock-toggle" aria-expanded="false" aria-controls="ccDockBody"><svg class="cc-svg cc-chev" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>
        <span class="cc-dock-title">Activity</span><span class="cc-dock-mini" data-part="mini"></span></button>
      <button type="button" class="cc-dock-stop-mini" hidden>STOP</button></div>
    <div class="cc-dock-body" id="ccDockBody">
      <p class="cc-dock-caption">${DOCK_CAPTION}</p>
      <div class="cc-dock-stage" data-part="stage"></div>
      <div class="cc-dock-steps"><ol class="cc-mdots" data-part="dots" role="img"></ol><span class="cc-dock-step" data-part="stepText"></span></div>
      <div class="cc-dock-fault" data-part="fault"></div>
      <ul class="cc-drows" data-part="rows" aria-label="Components: select one for details"></ul>
      <button type="button" class="cc-dock-stop" hidden>STOP EXPERIMENT</button>
      <ol class="cc-devents" data-part="events" aria-label="Most recent events, newest first"></ol></div>`;
  function createDock(el, { onStop, storage, narrowQuery = '(max-width: 899px)' } = {}) {
    const store = storage || (() => { try { return window.sessionStorage; } catch (e) { return null; } })();
    const mq = typeof window !== 'undefined' && window.matchMedia ? window.matchMedia(narrowQuery) : { matches: false };
    const key = () => mq.matches ? 'cc-dock-narrow' : 'cc-dock';
    const read = () => { try { return store && store.getItem(key()); } catch (e) { return null; } };
    const write = v => { try { store && store.setItem(key(), v); } catch (e) { /* the choice just is not remembered */ } };
    el.classList.add('cc-dock'); el.innerHTML = DOCK_SKELETON;
    const q = s => el.querySelector(s), cache = {}, openRows = new Set();
    const toggle = q('.cc-dock-toggle'), stopBtn = q('.cc-dock-stop'), miniStop = q('.cc-dock-stop-mini');
    let model = null;
    const isOpen = () => dockOpen(read(), model && model.active, mq.matches);
    function apply() {
      const open = isOpen();
      el.classList.toggle('open', open); el.classList.toggle('active', !!(model && model.active)); el.classList.toggle('running', !!(model && model.running));
      toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-label', open ? 'Collapse the activity panel' : 'Expand the activity panel');
      if (typeof document !== 'undefined') { document.body.classList.toggle('cc-dock-open', open); document.body.classList.toggle('cc-dock-pill', !open); }
    }
    const setPart = (name, html) => { if (cache[name] !== html) { cache[name] = html; q(`[data-part="${name}"]`).innerHTML = html; } };
    function paintRow(li) {                       // expanded: the full latest result plus description, file and implementation status
      const open = openRows.has(li.dataset.agent), lat = li.querySelector('.cc-drow-latest');
      li.classList.toggle('open', open); li.querySelector('.cc-drow-top').setAttribute('aria-expanded', String(open));
      if (lat) { lat.classList.toggle('open', open); lat.textContent = open ? lat.dataset.full : lat.dataset.short; }
    }
    const flip = li => { const id = li.dataset.agent; openRows.has(id) ? openRows.delete(id) : openRows.add(id); paintRow(li); };   // in place: keyboard focus stays on the row
    toggle.addEventListener('click', () => { write(isOpen() ? 'closed' : 'open'); apply(); });
    [stopBtn, miniStop].forEach(b => b.addEventListener('click', () => { if (onStop) onStop(); }));
    el.addEventListener('click', e => { const t = e.target.closest && e.target.closest('.cc-drow-top, .cc-drow-latest'); if (t) flip(t.closest('.cc-drow')); });
    el.addEventListener('keydown', e => { const t = e.target.closest && e.target.closest('.cc-drow-top'); if (t && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); flip(t.closest('.cc-drow')); } });
    if (mq.addEventListener) mq.addEventListener('change', apply);
    return { render(m, stop = {}) {
      model = m;
      const parts = dockParts(m);
      setPart('mini', parts.stage); setPart('stage', parts.stage); setPart('dots', parts.dots); setPart('stepText', parts.stepText); setPart('fault', parts.fault); setPart('events', parts.events || '');
      q('[data-part="stage"]').title = parts.headline; q('[data-part="dots"]').setAttribute('aria-label', parts.dotsLabel);
      if (cache.rows !== parts.rows) { cache.rows = parts.rows; q('[data-part="rows"]').innerHTML = parts.rows; }
      el.querySelectorAll('.cc-drow').forEach(paintRow);
      for (const b of [stopBtn, miniStop]) {
        b.hidden = !stop.visible; b.disabled = !!stop.disabled || !!stop.pending; b.classList.toggle('pending', !!stop.pending); b.setAttribute('aria-busy', String(!!stop.pending));
        b.textContent = stop.pending ? (b === miniStop ? 'Stopping…' : 'Stop requested: rolling back…') : (b === miniStop ? 'STOP' : 'STOP EXPERIMENT');
      }
      apply();
    }, apply };
  }

  const api = { esc, icon, chip, maturityChip, runtimeChip, buildSteps, mmss, dockOpen, dockParts, createDock, DOCK_CAPTION, MATURITY, RUNTIME };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.ChaosUI = api;
})(typeof window !== 'undefined' ? window : globalThis);
