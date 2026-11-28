# CDCI demo API

This service keeps the existing extraction scripts intact and orchestrates them through LangGraph.

## Windows PowerShell

From the repository root after installing Tesseract and Poppler as documented in \`PIPELINE_RUNBOOK.md\`:

\`\`\`powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
# Add GEMINI_KEY=your-key, save the file, then:
python -m uvicorn api.app:app --host 127.0.0.1 --port 8000
\`\`\`

Open \`http://127.0.0.1:8000/docs\`. Upload a drawing and optionally upload register/SOW workbooks. If omitted, the API uses the repository's sample Annexure files.

## macOS / Linux

\`\`\`bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Add GEMINI_KEY=your-key to .env, then:
python -m uvicorn api.app:app --host 127.0.0.1 --port 8000
\`\`\`

The UI is in \`web/\` and runs separately on port 3000.

The demo uses SQLite for job metadata and LangGraph checkpoints, local disk under \`runtime/jobs\`, and one active drawing at a time by default. The SQLite checkpointer is intended for lightweight/single-process demos, not horizontally scaled workers. Production needs PostgreSQL-backed metadata/checkpoints, object storage, a durable queue/worker pool, authentication/authorization, signed artifact URLs, secret management, distributed model rate limiting, and OpenTelemetry-compatible tracing/metrics. Do not expose the development API directly to the public internet.
