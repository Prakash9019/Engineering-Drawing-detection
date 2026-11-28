"""Single-host durable queue worker with lease heartbeats."""
from __future__ import annotations
import json, logging, os, socket, sqlite3, time, uuid, threading
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
from api.store import JobStore
from api.workflow import build_graph
ROOT=Path(__file__).resolve().parents[1]
load_dotenv(ROOT/".env",override=False)
RUNTIME=Path(os.getenv("CDCI_RUNTIME_DIR",ROOT/"runtime")).resolve()
RUNTIME.mkdir(parents=True,exist_ok=True)
WORKER_ID=f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
LOG=logging.getLogger("cdci.worker")
def _build_graph():
 connection=sqlite3.connect(RUNTIME/"langgraph-checkpoints.sqlite3",timeout=30,check_same_thread=False)
 from langgraph.checkpoint.sqlite import SqliteSaver
 saver=SqliteSaver(connection);saver.setup()
 return build_graph(saver),connection
def _heartbeat(store:JobStore,job_id:str,stop:threading.Event,lease:int):
 interval=max(10,lease//3)
 while not stop.wait(interval):
  if not store.heartbeat(job_id,WORKER_ID,lease):
   LOG.error("Lost lease for job %s",job_id);return
def run_once(store:JobStore,graph:Any)->bool:
 lease=int(os.getenv("CDCI_WORKER_LEASE_SECONDS","1800"))
 item=store.claim_next(WORKER_ID,lease)
 if item is None:return False
 job_id=item["job_id"];workspace=Path(item["workspace"]).resolve()
 payload=workspace/"worker_payload.json"
 if not payload.is_file():
  store.finish(job_id,WORKER_ID,"failed",message="Worker payload missing")
  store.event(job_id,"job.failed",error="Worker payload missing");return True
 stop=threading.Event()
 thread=threading.Thread(target=_heartbeat,args=(store,job_id,stop,lease),daemon=True)
 thread.start()
 try:
  state=json.loads(payload.read_text(encoding="utf-8"))
  store.event(job_id,"job.started",worker_id=WORKER_ID,attempt=item["attempts"])
  result=graph.invoke(state,config={"configurable":{"thread_id":job_id},"recursion_limit":40})
  artifacts=sorted(p.name for p in workspace.iterdir() if p.is_file() and p.name!="worker_payload.json")
  store.finish(job_id,WORKER_ID,"completed",message="Workflow completed",
               pair_evals=result.get("pair_evals",[]),artifacts=artifacts,
               extraction_scope=result.get("extraction_scope"))
  store.event(job_id,"job.completed",worker_id=WORKER_ID,artifact_count=len(artifacts))
 except Exception as exc:
  LOG.exception("Job %s failed",job_id)
  if item["attempts"]<int(os.getenv("CDCI_JOB_MAX_ATTEMPTS","2")):
   store.finish(job_id,WORKER_ID,"queued",message=str(exc),retrying=True)
   store.event(job_id,"job.retry_queued",attempt=item["attempts"],error=str(exc))
  else:
   store.finish(job_id,WORKER_ID,"failed",message=str(exc),retrying=False)
   store.event(job_id,"job.failed",error_type=type(exc).__name__,error=str(exc))
 finally:
  stop.set();thread.join(timeout=2)
 return True
def main():
 logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
 store=JobStore(RUNTIME/"jobs.sqlite3");graph,connection=_build_graph()
 poll=float(os.getenv("CDCI_WORKER_POLL_SECONDS","2"))
 LOG.warning("Worker %s started",WORKER_ID)
 try:
  while True:
   if not run_once(store,graph):time.sleep(poll)
 except KeyboardInterrupt:LOG.warning("Worker stopping")
 finally:connection.close()
if __name__=="__main__":main()
