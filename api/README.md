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

Set GEMINI_KEY and optional CDCI_API_KEY in root .env. Set CDCI_AUTH_REQUIRED=true to fail closed if the API key is absent. The API key is sent in X-API-Key headers.

## Endpoints
- POST /api/jobs — queue upload; accepts Idempotency-Key.
- GET /api/jobs/{id} — job state and history.
- GET /api/jobs/{id}/events — replayable SSE; supports Last-Event-ID.
- GET /api/jobs/{id}/files/{name} — allowlisted result file.
- POST /api/jobs/{id}/review — approve/reject/edit human queue item.
- GET /api/metrics — Prometheus text exposition.
- GET /api/health — liveness.

## Settings
CDCI_API_KEY, CDCI_AUTH_REQUIRED, CDCI_RUNTIME_DIR, CDCI_MAX_UPLOAD_BYTES,
CDCI_WORKER_POLL_SECONDS, CDCI_WORKER_LEASE_SECONDS, CDCI_JOB_MAX_ATTEMPTS,
CDCI_CORS_ORIGINS.

SQLite claiming uses BEGIN IMMEDIATE and is single-host/single-worker only. Do not share its database on a network filesystem. Multi-host production requires PostgreSQL state and checkpointer, Redis/SQS, object storage, OIDC/tenant authorization, signed URLs, secret manager, distributed rate limits, OTEL, backups, retention and dead-letter handling.
