# CDCI P&ID Drawing Intelligence

An engineering-drawing extraction pipeline that combines Gemini vision, Tesseract OCR, OpenCV geometry, structured Excel inputs, deterministic validation, and human review.

## Current implementation

- 12 logical processing stages wrap the existing step scripts; these are not 12 independent free-form LLM agents.
- LangGraph coordinates the stages through six paired workflow nodes and persists state with a SQLite checkpointer.
- FastAPI provides job upload, status, event and artifact endpoints.
- Next.js provides the recruiter demo dashboard with a 12-stage view, paired eval gates and result downloads.
- Deterministic checks validate artifact presence, JSON structure and candidate bounding boxes. The six eval gates make zero additional model calls.
- Model IDs can be routed per task through `CDCI_MODEL_*` environment variables.
- The original cloud-review browser editor and hierarchy viewer remain part of the workflow deliverables.

## Windows quick start

See [PIPELINE_RUNBOOK.md](PIPELINE_RUNBOOK.md) for the full PowerShell flow, including Tesseract and Poppler setup.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:GEMINI_KEY = "YOUR_GEMINI_API_KEY"
python -m uvicorn api.app:app --host 127.0.0.1 --port 8000
```

In another PowerShell window:

```powershell
cd web
npm install
$env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:8000"
npm run dev
```

Open `http://localhost:3000`. API docs: `http://127.0.0.1:8000/docs`.

## Architecture decisions

- **Workflow, not autonomous agent swarm:** the task has known dependencies, so LangGraph models deterministic transitions, a shared state, paired checks, and retry boundaries. The existing stage scripts remain the extraction source of truth.
- **Full-drawing hierarchy vs revision output:** hierarchy always runs from preserved full-sheet candidate and association artifacts. If drawing metadata specifies `CLOUD_ONLY`, extraction and validation also create a cloud-scoped deliverable path without overwriting the full hierarchy inputs.
- **No vector RAG at this stage:** Annexure-2 symbol scope, Annexure-4 register, drawing notes, and extracted tag tables are finite structured inputs. Existing deterministic lookup and structured JSON context are more appropriate than embedding them into a vector database. Revisit retrieval only when a large multi-project engineering knowledge corpus is introduced.
- **Observability:** per-stage structured events capture start/finish/failure, duration, exit code, retry count, model route, eval outcome, and job ID. Model token counts are not claimed because the legacy scripts do not expose consistent usage metadata yet.
- **Demo vs production:** SQLite/local disk and a single active job are for local recruiter demonstration. Production deployment needs PostgreSQL-backed metadata/checkpoints, object storage, a durable queue/worker pool, authentication/authorization, signed artifact URLs, secret management, distributed rate limiting, and OpenTelemetry-compatible tracing/metrics.

## Quality checks

```powershell
python -m compileall -q api stages core
python -m pytest -q
```

Cross-platform CI covers Windows and Ubuntu for the Python layer and builds the web app on Ubuntu.
