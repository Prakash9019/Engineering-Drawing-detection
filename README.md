# CDCI P&ID Drawing Intelligence

Engineering drawing extraction using Gemini vision, Tesseract OCR, OpenCV geometry, structured Excel inputs, deterministic validation and human review.

## What is implemented
- 12 logical extraction stages (existing Python scripts; not 12 separate free-form LLM agents).
- Branched LangGraph DAG: full-sheet hierarchy and conditional revision-scope deliverable path.
- FastAPI ingress plus a separate SQLite-backed worker queue for local single-host execution.
- Next.js recruiter dashboard with 12-stage progress, six paired evals, downloadable deliverables and human review decisions.
- Replayable SSE events, API-key dependency, idempotency keys, allowlisted downloads and Prometheus-style metrics.
- Six deterministic paired evals with zero additional model judge calls.
- Per-task model routing, bounded retries, LangGraph checkpoints and stage logs/JSONL events.

## Windows startup

Prepare Python and dependencies:
```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Terminal 1 — API:
```powershell
python -m uvicorn api.app:app --host 127.0.0.1 --port 8000
```
Terminal 2 — worker:
```powershell
python -m api.worker
```
Terminal 3 — UI:
```powershell
cd web
npm install
$env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:8000"
npm run dev
```

Open http://localhost:3000. Configure GEMINI_KEY in .env. Set CDCI_API_KEY in .env when using API authentication.

## Quality and architecture
The six eval gates check artifact contracts, context, candidate geometry/IDs, hierarchy, and unique review IDs. They are contract/smoke checks, not semantic accuracy metrics. Accuracy requires a labelled drawing corpus and ground-truth comparisons.

No vector RAG is used for the current bounded Excel/JSON inputs. Deterministic lookups are simpler and cheaper; reconsider retrieval for large cross-project engineering document collections.

## Deployment boundary
The SQLite queue is deliberately single-host/single-worker. Production multi-host requires PostgreSQL/Redis or SQS, PostgreSQL LangGraph checkpoints, object storage, OIDC and tenant authorization, signed artifact URLs, secret manager, distributed model rate limiting, OTEL traces/metrics, malware scanning, backups, retention and dead-letter processing.

Run tests with python -m pytest -q and build/typecheck the web app with npm run typecheck and npm run build in web/.
