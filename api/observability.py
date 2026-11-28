"""Structured workflow observability with safe event persistence."""
from __future__ import annotations
import json,logging,os,time
from datetime import datetime,timezone
from pathlib import Path
from typing import Any,Callable
from api.store import JobStore
_LOG=logging.getLogger("cdci.workflow")
if not _LOG.handlers:
 h=logging.StreamHandler();h.setFormatter(logging.Formatter("%(message)s"));_LOG.addHandler(h);_LOG.setLevel(logging.INFO)
def emit_event(event:str,**fields:Any)->dict[str,Any]:
 record={"timestamp":datetime.now(timezone.utc).isoformat(),"service":"cdci-orchestrator","event":event,**fields}
 _LOG.info(json.dumps(record,default=str,separators=(",",":")));return record
def timed(name:str,fn:Callable[[],Any],**fields):
 start=time.perf_counter();emit_event("stage.started",stage=name,**fields)
 try: result=fn()
 except Exception as exc:
  emit_event("stage.failed",stage=name,duration_seconds=round(time.perf_counter()-start,3),
             error_type=type(exc).__name__,error=str(exc),**fields);raise
 elapsed=round(time.perf_counter()-start,3);emit_event("stage.completed",stage=name,duration_seconds=elapsed,**fields)
 return result,elapsed
def append_jsonl(path:Path,record:dict[str,Any])->None:
 path.parent.mkdir(parents=True,exist_ok=True)
 with path.open("a",encoding="utf-8") as stream:stream.write(json.dumps(record,default=str)+os.linesep)

def emit_job_event(store:JobStore,workspace:Path,event:str,**fields:Any)->dict[str,Any]:
 record=emit_event(event,**fields)
 store.event(str(fields.get("job_id","")),event,**{k:v for k,v in fields.items() if k!="job_id"})
 append_jsonl(workspace/"events.jsonl",{"event":event,**fields})
 return record
