"""Single-host durable queue worker for the local CDCI demo."""
from __future__ import annotations
import json, logging, os, socket, sqlite3, time, uuid
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
    saver=SqliteSaver(connection); saver.setup()
    return build_graph(saver),connection
def run_once(store: JobStore, graph: Any) -> bool:
    lease=int(os.getenv("CDCI_WORKER_LEASE_SECONDS","900"))
    item=store.claim_next(WORKER_ID,lease)
    if item is None: return False
    job_id=item["job_id"]; workspace=Path(item["workspace"]).resolve()
    payload_path=workspace/"worker_payload.json"
    if not payload_path.is_file():
        store.finish(job_id,WORKER_ID,"failed",message="Worker payload missing")
        store.event(job_id,"job.failed",error="Worker payload missing")
        return True
    try:
        state=json.loads(payload_path.read_text(encoding="utf-8"))
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
    return True
def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
    store=JobStore(RUNTIME/"jobs.sqlite3"); graph,connection=_build_graph()
    poll=float(os.getenv("CDCI_WORKER_POLL_SECONDS","2"))
    LOG.warning("Worker %s started",WORKER_ID)
    try:
        while True:
            if not run_once(store,graph): time.sleep(poll)
    except KeyboardInterrupt: LOG.warning("Worker stopping")
    finally: connection.close()
if __name__=="__main__": main()
