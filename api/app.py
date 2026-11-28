"""FastAPI ingress and read API. Workflow execution runs in api.worker."""
from __future__ import annotations
import json, os, re, uuid, asyncio
from pathlib import Path
from typing import Optional
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, Response
from dotenv import load_dotenv
from api.store import JobStore
from api.security import require_api_key
from api.metrics import render_metrics
from api.schemas import ReviewDecision

ROOT=Path(__file__).resolve().parents[1]
load_dotenv(ROOT/".env",override=False)
RUNTIME=Path(os.getenv("CDCI_RUNTIME_DIR",ROOT/"runtime")).resolve()
RUNTIME.mkdir(parents=True,exist_ok=True)
STORE=JobStore(RUNTIME/"jobs.sqlite3")
MAX_UPLOAD=int(os.getenv("CDCI_MAX_UPLOAD_BYTES",str(50*1024*1024)))
ALLOWED_DRAWINGS={".pdf",".png",".jpg",".jpeg",".tif",".tiff"}
ARTIFACT_ALLOWLIST={
 "final_tags.xlsx","final_hierarchy.xlsx","hierarchy_viewer.html","hierarchy_graph.html",
 "hierarchy_validation_report.xlsx","eval_coverage_report.json","final_tags_vs_annexure4.xlsx",
 "human_review_queue.json","audit_log.json","step8_routing_summary.json"
}
app=FastAPI(title="CDCI Drawing Intelligence API",version="0.2.0")
app.add_middleware(
 CORSMiddleware,
 allow_origins=[x.strip() for x in os.getenv("CDCI_CORS_ORIGINS","http://localhost:3000").split(",") if x.strip()],
 allow_credentials=True,allow_methods=["GET","POST"],allow_headers=["Content-Type","X-API-Key","Idempotency-Key","Last-Event-ID"]
)

def _safe_name(name:str)->str:
 return re.sub(r"[^A-Za-z0-9._-]","_",Path(name or "upload").name)[:120] or "upload"

async def _save_upload(file:UploadFile,destination:Path,allowed:set[str]):
 suffix=Path(file.filename or "").suffix.lower()
 if suffix not in allowed: raise HTTPException(415,f"Unsupported file type: {suffix or 'none'}")
 total=0; destination.parent.mkdir(parents=True,exist_ok=True)
 with destination.open("wb") as out:
  while chunk:=await file.read(1024*1024):
   total+=len(chunk)
   if total>MAX_UPLOAD:
    destination.unlink(missing_ok=True); raise HTTPException(413,"Upload exceeds configured size limit")
   out.write(chunk)
 if total==0:
  destination.unlink(missing_ok=True); raise HTTPException(400,"Uploaded file is empty")

@app.get("/api/health")
def health():
 return {"status":"ok","service":"cdci-api","queue":"sqlite"}

@app.get("/api/metrics",dependencies=[Depends(require_api_key)])
def metrics():
 return Response(render_metrics(STORE.list(500)),media_type="text/plain; version=0.0.4")

@app.post("/api/jobs",status_code=202)
async def create_job(
 drawing:UploadFile=File(...),register:Optional[UploadFile]=File(None),
 sow:Optional[UploadFile]=File(None),workers:int=Form(1),
 idempotency_key:Optional[str]=Header(default=None,alias="Idempotency-Key"),
 _:str=Depends(require_api_key)
):
 if not (os.getenv("GEMINI_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")):
  raise HTTPException(400,"Set GEMINI_KEY on the worker environment.")
 if not drawing.filename: raise HTTPException(400,"A drawing file is required.")
 if idempotency_key and len(idempotency_key)>200: raise HTTPException(400,"Idempotency-Key is too long.")
 job_id=uuid.uuid4().hex
 workspace=(RUNTIME/"jobs"/job_id).resolve()
 upload_dir=workspace/"uploads"; upload_dir.mkdir(parents=True,exist_ok=False)
 drawing_path=upload_dir/("drawing__"+_safe_name(drawing.filename))
 try:
  await _save_upload(drawing,drawing_path,ALLOWED_DRAWINGS)
  register_default=ROOT/"ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx"
  sow_default=ROOT/"ANNEXURE-2_CDC-SYMBOLS_USED_AND_NOT_USED.xlsx"
  if register is not None:
   register_path=upload_dir/("register__"+_safe_name(register.filename or "register.xlsx"))
   await _save_upload(register,register_path,{".xlsx",".xlsm"})
  else: register_path=register_default
  if sow is not None:
   sow_path=upload_dir/("sow__"+_safe_name(sow.filename or "scope.xlsx"))
   await _save_upload(sow,sow_path,{".xlsx",".xlsm"})
  else: sow_path=sow_default
  if not register_path.is_file(): raise HTTPException(400,"Upload Annexure-4 register; repository default is missing.")
  if not sow_path.is_file(): raise HTTPException(400,"Upload SOW workbook; repository default is missing.")
  state={"job_id":job_id,"workspace":str(workspace),"drawing_path":str(drawing_path),
         "register_path":str(register_path),"sow_path":str(sow_path),
         "workers":max(1,min(workers,8)),"pair_evals":[]}
  (workspace/"worker_payload.json").write_text(json.dumps(state),encoding="utf-8")
  job,created=STORE.create(job_id,workspace,drawing_path.name,idempotency_key)
  if not created:
   # A duplicate request reuses the earlier job; its new temporary input folder is discarded.
   import shutil
   shutil.rmtree(workspace,ignore_errors=True)
   return {"job_id":job["job_id"],"status":job["status"],"status_url":f"/api/jobs/{job['job_id']}"}
  return {"job_id":job_id,"status":"queued","status_url":f"/api/jobs/{job_id}"}
 except Exception:
  import shutil
  shutil.rmtree(workspace,ignore_errors=True)
  raise

@app.get("/api/jobs",dependencies=[Depends(require_api_key)])
def list_jobs():
 return {"jobs":STORE.list(100)}

@app.get("/api/jobs/{job_id}",dependencies=[Depends(require_api_key)])
def get_job(job_id:str):
 job=STORE.get(job_id)
 if job is None: raise HTTPException(404,"Job not found")
 return job

@app.get("/api/jobs/{job_id}/events",dependencies=[Depends(require_api_key)])
async def stream_events(job_id:str,last_event_id:Optional[str]=Header(default=None,alias="Last-Event-ID")):
 if STORE.get(job_id) is None: raise HTTPException(404,"Job not found")
 try: cursor=max(0,int(last_event_id or "0"))
 except ValueError: cursor=0
 async def generate():
  nonlocal cursor
  while True:
   rows=STORE.events_after(job_id,cursor)
   for item in rows:
    eid=item.pop("event_id")
    cursor=eid
    yield f"id: {eid}\nevent: {item.get('event','message')}\ndata: {json.dumps(item,default=str)}\n\n"
   job=STORE.get(job_id)
   if job and job["status"] in {"completed","failed","cancelled"} and not rows:
    yield "event: stream.end\ndata: {}\n\n"; break
   if await asyncio.sleep(0.75) is None: pass
 return StreamingResponse(generate(),media_type="text/event-stream",
                          headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.get("/api/jobs/{job_id}/files/{filename}",dependencies=[Depends(require_api_key)])
def download_artifact(job_id:str,filename:str):
 if filename not in ARTIFACT_ALLOWLIST: raise HTTPException(404,"Artifact not found")
 job=STORE.get(job_id)
 if job is None: raise HTTPException(404,"Job not found")
 if job["status"]!="completed": raise HTTPException(409,"Artifacts are not ready")
 workspace=Path(job["workspace"]).resolve()
 target=(workspace/filename).resolve()
 if target.parent!=workspace or not target.is_file(): raise HTTPException(404,"Artifact not found")
 return FileResponse(target,filename=target.name)

@app.post("/api/jobs/{job_id}/review",dependencies=[Depends(require_api_key)])
def review(job_id:str,body:ReviewDecision):
 try:
  result=STORE.decide_review(job_id,body.decision_id,body.decision,body.reviewer,body.note)
  return {"status":"recorded","item":result}
 except KeyError: raise HTTPException(404,"Job or review item not found")
 except FileNotFoundError as exc: raise HTTPException(409,str(exc))
 except ValueError as exc: raise HTTPException(422,str(exc))
