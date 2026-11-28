"""FastAPI entrypoint for the CDCI recruiter demonstration."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from dotenv import load_dotenv

from api.observability import emit_event
from api.store import JobStore
from api.workflow import build_graph

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
RUNTIME = Path(os.getenv("CDCI_RUNTIME_DIR", ROOT / "runtime")).resolve()
RUNTIME.mkdir(parents=True, exist_ok=True)
STORE = JobStore(RUNTIME / "jobs.sqlite3")
CHECKPOINTS = sqlite3.connect(RUNTIME / "langgraph-checkpoints.sqlite3", check_same_thread=False)
try:
    from langgraph.checkpoint.sqlite import SqliteSaver
    CHECKPOINTER = SqliteSaver(CHECKPOINTS)
    CHECKPOINTER.setup()
except Exception as exc:
    raise RuntimeError(
        "LangGraph SQLite checkpointer is unavailable. Install requirements.txt."
    ) from exc

GRAPH = build_graph(CHECKPOINTER)
_ALLOWED_DRAWINGS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
_MAX_UPLOAD = int(os.getenv("CDCI_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
_ACTIVE = threading.BoundedSemaphore(max(1, int(os.getenv("CDCI_MAX_CONCURRENT_JOBS", "1"))))

app = FastAPI(title="CDCI Drawing Intelligence API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CDCI_CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name or "upload").name)
    return cleaned[:120] or "upload"


async def _save_upload(file: UploadFile, destination: Path, allowed: set[str] | None = None) -> None:
    suffix = Path(file.filename or "").suffix.lower()
    if allowed is not None and suffix not in allowed:
        raise HTTPException(415, f"Unsupported file type: {suffix or 'no extension'}")
    total = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > _MAX_UPLOAD:
                output.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(413, "Upload exceeds configured size limit")
            output.write(chunk)
    if total == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(400, "Uploaded file is empty")


def _run_job(job_id: str, initial_state: dict) -> None:
    _ACTIVE.acquire()
    try:
        STORE.update(job_id, status="running", message="Workflow started")
        STORE.event(job_id, "job.started")
        config = {"configurable": {"thread_id": job_id}, "recursion_limit": 40}
        result = GRAPH.invoke(initial_state, config=config)
        workspace = Path(initial_state["workspace"])
        artifacts = sorted(
            path.name for path in workspace.iterdir()
            if path.is_file() and path.name not in {"drawing_context.json"}
        )
        STORE.update(job_id, status="completed", message="All stages and eval gates passed",
                     pair_evals=result.get("pair_evals", []), artifacts=artifacts,
                     extraction_scope=result.get("extraction_scope"))
        STORE.event(job_id, "job.completed", artifact_count=len(artifacts))
    except Exception as exc:
        emit_event("job.failed", job_id=job_id, error_type=type(exc).__name__, error=str(exc))
        STORE.update(job_id, status="failed", message=str(exc))
        STORE.event(job_id, "job.failed", error=str(exc))
    finally:
        _ACTIVE.release()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "cdci-api", "orchestrator": "langgraph"}


@app.post("/api/jobs", status_code=202)
async def create_job(
    background_tasks: BackgroundTasks,
    drawing: UploadFile = File(...),
    register: Optional[UploadFile] = File(None),
    sow: Optional[UploadFile] = File(None),
    workers: int = Form(1),
) -> dict:
    api_key = os.getenv("GEMINI_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(400, "Set GEMINI_KEY, GOOGLE_API_KEY, or GEMINI_API_KEY on the API server.")
    if not drawing.filename:
        raise HTTPException(400, "A drawing file is required.")
    job_id = uuid.uuid4().hex
    workspace = (RUNTIME / "jobs" / job_id).resolve()
    workspace.mkdir(parents=True, exist_ok=False)
    upload_dir = workspace / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    drawing_path = upload_dir / ("drawing__" + _safe_name(drawing.filename))
    await _save_upload(drawing, drawing_path, _ALLOWED_DRAWINGS)

    register_default = ROOT / "ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx"
    sow_default = ROOT / "ANNEXURE-2_CDC-SYMBOLS_USED_AND_NOT_USED.xlsx"
    if register is not None:
        register_path = upload_dir / ("register__" + _safe_name(register.filename or "register.xlsx"))
        await _save_upload(register, register_path, {".xlsx", ".xlsm"})
    else:
        register_path = register_default
    if sow is not None:
        sow_path = upload_dir / ("sow__" + _safe_name(sow.filename or "scope.xlsx"))
        await _save_upload(sow, sow_path, {".xlsx", ".xlsm"})
    else:
        sow_path = sow_default

    if not register_path.is_file():
        raise HTTPException(400, "Upload an Annexure-4 register; no default register was found.")
    if not sow_path.is_file():
        raise HTTPException(400, "Upload an SOW symbol workbook; no default workbook was found.")

    STORE.create(job_id, workspace, drawing_path.name)
    state = {
        "job_id": job_id,
        "workspace": str(workspace),
        "drawing_path": str(drawing_path),
        "register_path": str(register_path),
        "sow_path": str(sow_path),
        "workers": max(1, min(workers, 8)),
        "pair_evals": [],
    }
    background_tasks.add_task(_run_job, job_id, state)
    return {"job_id": job_id, "status": "queued", "status_url": f"/api/jobs/{job_id}"}


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": STORE.list()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = STORE.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    events_file = Path(job["workspace"]) / "events.jsonl"
    if events_file.is_file():
        try:
            job["events"] = [
                json.loads(line)
                for line in events_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, ValueError):
            pass
    return job


@app.get("/api/jobs/{job_id}/files/{filename}")
def download_artifact(job_id: str, filename: str):
    job = STORE.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    workspace = Path(job["workspace"]).resolve()
    target = (workspace / filename).resolve()
    if target.parent != workspace or not target.is_file():
        raise HTTPException(404, "Artifact not found")
    return FileResponse(target, filename=target.name)
