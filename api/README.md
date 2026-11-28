# CDCI API and worker

The API persists uploads and queues jobs in SQLite. A separate worker process claims and executes LangGraph workflows.

## Windows PowerShell

Terminal 1 — API:
\`\`\`powershell
python -m uvicorn api.app:app --host 127.0.0.1 --port 8000
\`\`\`
Terminal 2 — worker:
\`\`\`powershell
python -m api.worker
\`\`\`
Terminal 3 — frontend:
\`\`\`powershell
cd web
npm install
$env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:8000"
$env:NEXT_PUBLIC_CDCI_API_KEY = "YOUR_CDCI_API_KEY"
npm run dev
\`\`\`
Set GEMINI_KEY and optional CDCI_API_KEY in root .env. When configured, the UI sends X-API-Key.

## API endpoints
- POST /api/jobs: upload and queue; supports Idempotency-Key.
- GET /api/jobs/{id}: status and events.
- GET /api/jobs/{id}/events: replayable Server-Sent Events using Last-Event-ID.
- GET /api/jobs/{id}/files/{name}: allowlisted artifact download.
- POST /api/jobs/{id}/review: approve/reject/edit a human-review item.
- GET /api/metrics: Prometheus text exposition.
- GET /api/health: liveness.

## Settings
CDCI_API_KEY, CDCI_AUTH_REQUIRED, CDCI_RUNTIME_DIR, CDCI_MAX_UPLOAD_BYTES,
CDCI_WORKER_POLL_SECONDS, CDCI_WORKER_LEASE_SECONDS, CDCI_JOB_MAX_ATTEMPTS,
CDCI_CORS_ORIGINS.

SQLite claiming uses BEGIN IMMEDIATE and is single-host/single-worker only. Do not share the SQLite file over a network filesystem. Multi-host production requires PostgreSQL state and checkpointer, Redis/SQS queue, object storage, OIDC/tenant authorization, signed URLs, secrets manager, distributed rate limiting, OTEL and retention/backup/dead-letter processing.
