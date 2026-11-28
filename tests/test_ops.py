from api.metrics import render_metrics
from api.security import require_api_key
from fastapi import HTTPException

def test_metrics_render_job_status(tmp_path):
    job={"status":"completed","workspace":str(tmp_path)}
    text=render_metrics([job])
    assert 'cdci_jobs_total{status="completed"} 1' in text
    assert "cdci_stage_duration_seconds_count" in text

def test_auth_rejects_wrong_key(monkeypatch):
    monkeypatch.setenv("CDCI_API_KEY","secret")
    try:
        require_api_key("wrong")
    except HTTPException as exc:
        assert exc.status_code==401
    else:
        raise AssertionError("Expected auth failure")

def test_auth_accepts_correct_key(monkeypatch):
    monkeypatch.setenv("CDCI_API_KEY","secret")
    assert require_api_key("secret")=="authenticated"
