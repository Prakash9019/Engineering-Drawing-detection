import json
from pathlib import Path
from api.store import JobStore

def test_claim_queued_job_is_atomic_and_leased(tmp_path):
    store=JobStore(tmp_path/"jobs.sqlite3")
    workspace=tmp_path/"job";workspace.mkdir()
    store.create("job-1",workspace,"drawing.pdf")
    item=store.claim_next("worker-a",lease_seconds=90)
    assert item and item["job_id"]=="job-1"
    assert item["attempts"]==1
    assert store.claim_next("worker-b",90) is None
    assert store.heartbeat("job-1","worker-a",90)

def test_job_events_are_replayable_by_id(tmp_path):
    store=JobStore(tmp_path/"jobs.sqlite3")
    workspace=tmp_path/"job";workspace.mkdir()
    store.create("job-2",workspace,"drawing.png")
    store.event("job-2","stage.started",step="extract")
    first=store.events_after("job-2",0)
    assert len(first)>=2
    last=first[-1]["event_id"]
    store.event("job-2","stage.completed",step="extract")
    later=store.events_after("job-2",last)
    assert len(later)==1
    assert later[0]["event"]=="stage.completed"

def test_idempotency_key_returns_existing_job(tmp_path):
    store=JobStore(tmp_path/"jobs.sqlite3")
    one=store.create("job-1",tmp_path/"one","one.pdf","request-123")
    two=store.create("job-2",tmp_path/"two","two.pdf","request-123")
    assert one[1] is True
    assert two[1] is False
    assert two[0]["job_id"]=="job-1"

def test_human_review_records_actual_queue_id(tmp_path):
    store=JobStore(tmp_path/"jobs.sqlite3")
    workspace=tmp_path/"job";workspace.mkdir()
    (workspace/"human_review_queue.json").write_text(json.dumps({"items":[
      {"queue_id":"CANON-42","candidate_id":"cand-1","tag_text":"PIT-42"}
    ]}),encoding="utf-8")
    store.create("job-3",workspace,"drawing.pdf")
    result=store.decide_review("job-3","CANON-42","approve","qa-user","checked")
    assert result["human_decision"]=="approve"
    updated=json.loads((workspace/"human_review_queue.json").read_text(encoding="utf-8"))
    assert updated["items"][0]["human_reviewer"]=="qa-user"
