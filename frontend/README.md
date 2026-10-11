# Chaos Copilot frontend

A dependency-free, single-screen operator console for the Chaos Copilot MVP.
It implements the six UI zones in `CHAOS_COPILOT_PRD.md`: readiness, topology,
proposal, policy, live run, and findings.

## Run the integrated application

```powershell
cd ..
.\.venv\Scripts\Activate.ps1
python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

Then open <http://127.0.0.1:8000>. FastAPI serves this directory and the PRD's
relative `/api/*` endpoints from the same origin.

For frontend-only styling work, `python -m http.server 4173` still works. In
that mode the UI calls `http://127.0.0.1:8000`. Set
`window.CHAOS_COPILOT_API_URL` before `app.js` to override the API origin.

### Offline demo

The offline demo is explicit: open the page with `?demo=1`. Every number is then
sample data and a banner says so. Without the flag the page never shows sample
data: if the API is unreachable it shows an "API unreachable" state (readiness
blocked, components "Not connected", metrics "No data") instead of pretending.

## Design system and component status

`chaos-ui.css`, `chaos-ui.js` and `component-meta.json` are shared with the
`app.py` UI (`static/`); keep the two copies identical (`tests/test_design_system.py`
fails when they drift).

- **Agent & Workflow Activity** shows six components. *Implementation status*
  (implemented / in development / planned) comes only from `component-meta.json`;
  *runtime status* (idle, running, waiting for approval, completed, blocked, failed)
  comes only from the API state, the policy response and the SSE events
  (`js/workflow.js`). Update `component-meta.json` when a component's maturity changes.
- The lifecycle stepper follows the backend's own experiment states; an aborted or
  failed run never renders as completed.

## Backend integration

Serve this directory from the FastAPI origin (recommended), or reverse-proxy
`/api` to the backend. The adapter accepts the endpoint contract from PRD
section 14 and tolerates common response aliases while the backend models are
being finalized.

Mutating calls include an `Idempotency-Key` header. The UI never accepts or
sends shell commands, requires an explicit checkbox before approval, and keeps
the emergency stop available throughout active experiment phases.

## Verify

```powershell
npm test
npm run check
```

There are no runtime or development dependencies to install.
