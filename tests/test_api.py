import io
from pathlib import Path
from fastapi.testclient import TestClient
import api.app as app_module


def setup_api(tmp_path, monkeypatch):
    from api.store import JobStore
    monkeypatch.setattr(app_module,"RUNTIME",tmp_path)
    monkeypatch.setattr(app_module,"STORE",JobStore(tmp_path/"jobs.sqlite3"))
    monkeypatch.setattr(app_module,"MAX_UPLOAD",1024*1024)
    monkeypatch.setenv("GEMINI_KEY","test-key")
    monkeypatch.setenv("CDCI_API_KEY","")
    monkeypatch.setenv("CDCI_AUTH_REQUIRED","false")


def test_health_is_available():
    client=TestClient(app_module.app)
    response=client.get("/api/health")
    assert response.status_code==200
    assert response.json()["status"]=="ok"


def test_api_key_dependency_fails_closed(monkeypatch):
    from api.security import require_api_key
    from fastapi import HTTPException
    monkeypatch.setenv("CDCI_API_KEY","strong-secret")
    try:
        require_api_key("wrong")
    except HTTPException as exc:
        assert exc.status_code==401
    else:
        raise AssertionError("Expected 401")


def test_upload_queues_durable_job_and_idempotency(monkeypatch,tmp_path):
    setup_api(tmp_path,monkeypatch)
    client=TestClient(app_module.app)
    files={"drawing":("p&id.png",io.BytesIO(b"fake-png-content"),"image/png")}
    response=client.post("/api/jobs",files=files,headers={"Idempotency-Key":"recruiter-upload-1"})
    assert response.status_code==202
    job_id=response.json()["job_id"]
    job=app_module.STORE.get(job_id)
    assert job["status"]=="queued"
    assert (Path(job["workspace"])/"worker_payload.json").is_file()

    files2={"drawing":("other.png",io.BytesIO(b"different"),"image/png")}
    duplicate=client.post("/api/jobs",files=files2,headers={"Idempotency-Key":"recruiter-upload-1"})
    assert duplicate.status_code==200 or duplicate.status_code==202
    assert duplicate.json()["job_id"]==job_id


def test_artifact_download_allowlist(monkeypatch,tmp_path):
    setup_api(tmp_path,monkeypatch)
    client=TestClient(app_module.app)
    assert client.get("/api/jobs/none/files/../../jobs.sqlite3").status_code==404


def test_sse_replays_durable_events(monkeypatch,tmp_path):
    setup_api(tmp_path,monkeypatch)
    workspace=tmp_path/"job-sse";workspace.mkdir()
    app_module.STORE.create("sse-job",workspace,"drawing.png")
    app_module.STORE.event("sse-job","test.event",step="test")
    client=TestClient(app_module.app)
    response=client.get("/api/jobs/sse-job/events",headers={"Last-Event-ID":"0"})
    assert response.status_code==200
    assert "test.event" in response.text
