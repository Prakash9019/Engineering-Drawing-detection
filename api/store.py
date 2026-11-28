"""SQLite durable queue and event store for a single-host demo."""
from __future__ import annotations
import json, sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
def utcnow(): return datetime.now(timezone.utc).isoformat()
class JobStore:
    def __init__(self,database:Path):
        self.database=database; database.parent.mkdir(parents=True,exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL"); db.execute("PRAGMA foreign_keys=ON")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs(
             job_id TEXT PRIMARY KEY,status TEXT NOT NULL,workspace TEXT NOT NULL,input_name TEXT NOT NULL,
             created_at TEXT NOT NULL,updated_at TEXT NOT NULL,payload TEXT NOT NULL,
             lease_owner TEXT,lease_until TEXT,attempts INTEGER NOT NULL DEFAULT 0,idempotency_key TEXT UNIQUE)""")
            db.execute("""CREATE TABLE IF NOT EXISTS job_events(
             id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL,created_at TEXT NOT NULL,
             payload TEXT NOT NULL,FOREIGN KEY(job_id) REFERENCES jobs(job_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status,created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id,id)")
    @contextmanager
    def _connect(self)->Iterator[sqlite3.Connection]:
        db=sqlite3.connect(self.database,timeout=30); db.row_factory=sqlite3.Row
        try: yield db; db.commit()
        except Exception: db.rollback(); raise
        finally: db.close()
    def create(self,job_id:str,workspace:Path,input_name:str,idempotency_key:str|None=None):
        now=utcnow(); payload={"current_step":None,"steps":[],"evals":[],"artifacts":[]}
        with self._connect() as db:
            if idempotency_key:
                row=db.execute("SELECT job_id FROM jobs WHERE idempotency_key=?",(idempotency_key,)).fetchone()
                if row: return self.get(row["job_id"]),False
            db.execute("""INSERT INTO jobs(job_id,status,workspace,input_name,created_at,updated_at,payload,idempotency_key)
             VALUES(?,?,?,?,?,?,?,?)""",(job_id,"queued",str(workspace),input_name,now,now,json.dumps(payload),idempotency_key))
        self.event(job_id,"job.queued",input_name=input_name); return self.get(job_id),True
    def update(self,job_id:str,status:str|None=None,**fields):
        with self._connect() as db:
            row=db.execute("SELECT payload,status FROM jobs WHERE job_id=?",(job_id,)).fetchone()
            if row is None: raise KeyError(job_id)
            payload=json.loads(row["payload"]); payload.update(fields)
            db.execute("UPDATE jobs SET status=?,payload=?,updated_at=? WHERE job_id=?",
             (status or row["status"],json.dumps(payload,default=str),utcnow(),job_id))
    def event(self,job_id:str,event:str,**fields):
        record={"event":event,"timestamp":utcnow(),**fields}
        with self._connect() as db:
            db.execute("INSERT INTO job_events(job_id,created_at,payload) VALUES(?,?,?)",
             (job_id,record["timestamp"],json.dumps(record,default=str)))
        return record
    def events_after(self,job_id:str,event_id:int=0,limit:int=250):
        with self._connect() as db:
            rows=db.execute("SELECT id,payload FROM job_events WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
             (job_id,event_id,limit)).fetchall()
        items=[]
        for row in rows:
            item=json.loads(row["payload"]); item["event_id"]=row["id"]; items.append(item)
        return items
    def claim_next(self,worker_id:str,lease_seconds:int=900):
        now=datetime.now(timezone.utc); iso=now.isoformat(); until=(now+timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("""SELECT job_id,payload,workspace,input_name,attempts FROM jobs
             WHERE status='queued' OR (status='running' AND lease_until IS NOT NULL AND lease_until < ?)
             ORDER BY created_at LIMIT 1""",(iso,)).fetchone()
            if row is None:return None
            cur=db.execute("""UPDATE jobs SET status='running',lease_owner=?,lease_until=?,attempts=attempts+1,updated_at=?
             WHERE job_id=? AND (status='queued' OR lease_until < ?)""",(worker_id,until,iso,row["job_id"],iso))
            if cur.rowcount!=1:return None
            return {"job_id":row["job_id"],"payload":json.loads(row["payload"]),
             "workspace":row["workspace"],"input_name":row["input_name"],"attempts":row["attempts"]+1}
    def finish(self,job_id:str,worker_id:str,status:str,**fields):
        with self._connect() as db:
            row=db.execute("SELECT payload FROM jobs WHERE job_id=?",(job_id,)).fetchone()
            if row is None:raise KeyError(job_id)
            payload=json.loads(row["payload"]); payload.update(fields)
            db.execute("""UPDATE jobs SET status=?,payload=?,lease_owner=NULL,lease_until=NULL,updated_at=?
             WHERE job_id=? AND lease_owner=?""",(status,json.dumps(payload,default=str),utcnow(),job_id,worker_id))
    def heartbeat(self,job_id:str,worker_id:str,lease_seconds:int=900):
        until=(datetime.now(timezone.utc)+timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as db:
            cur=db.execute("""UPDATE jobs SET lease_until=?,updated_at=? WHERE job_id=? AND lease_owner=? AND status='running'""",
             (until,utcnow(),job_id,worker_id)); return cur.rowcount==1
    def get(self,job_id:str):
        with self._connect() as db:
            row=db.execute("SELECT * FROM jobs WHERE job_id=?",(job_id,)).fetchone()
            if row is None:return None
            result=dict(row); result["payload"]=json.loads(result["payload"])
            result["events"]=self.events_after(job_id); return result
    def list(self,limit:int=25):
        with self._connect() as db:
            rows=db.execute("SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()
        return [j for row in rows if (j:=self.get(row["job_id"])) is not None]
    def decide_review(self,job_id:str,decision_id:str,decision:str,reviewer:str,note:str|None=None):
        if decision not in {"approve","reject","edit"}:raise ValueError("Invalid decision")
        job=self.get(job_id)
        if job is None:raise KeyError(job_id)
        path=Path(job["workspace"])/"human_review_queue.json"
        if not path.is_file():raise FileNotFoundError("Review queue not found")
        queue=json.loads(path.read_text(encoding="utf-8")); records=queue.get("items",[]) if isinstance(queue,dict) else queue
        target=next((r for r in records if str(r.get("queue_id") or r.get("candidate_id") or r.get("tag"))==decision_id),None)
        if target is None:raise KeyError(decision_id)
        target.update({"human_decision":decision,"human_reviewer":reviewer,"human_note":note or "","reviewed_at":utcnow()})
        path.write_text(json.dumps(queue,indent=2),encoding="utf-8")
        self.event(job_id,"human_review.decision",decision_id=decision_id,decision=decision,reviewer=reviewer,note=note or "")
        self.update(job_id,review_updated_at=utcnow()); return target
