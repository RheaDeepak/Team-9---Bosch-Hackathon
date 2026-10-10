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
that mode the UI automatically calls `http://127.0.0.1:8000`. If the API is
not running, it switches to clearly labelled demo data. Set
`window.CHAOS_COPILOT_API_URL` before `app.js` to override the API origin.

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
