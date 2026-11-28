import json

from api.evals import evaluate_pair
from api.model_routing import apply_model_routing
from api.store import JobStore
from api.workflow import build_graph


def test_eval_passes_when_paired_artifacts_exist(tmp_path):
    (tmp_path / "drawing_context.json").write_text(json.dumps({"drawing": "raster", "drawing_number": "D-001", "revision_code": "C"}), encoding="utf-8")
    (tmp_path / "title_block_context.json").write_text(json.dumps({"revision": "C"}), encoding="utf-8")

    result = evaluate_pair("eval_01_format_title", tmp_path)

    assert result["status"] == "PASS"
    assert result["model_calls"] == 0
    assert result["missing_artifacts"] == []


def test_eval_fails_on_missing_artifact(tmp_path):
    (tmp_path / "drawing_context.json").write_text("{}", encoding="utf-8")

    result = evaluate_pair("eval_01_format_title", tmp_path)

    assert result["status"] == "FAIL"
    assert result["missing_artifacts"] == ["title_block_context.json"]


def test_eval_rejects_malformed_json(tmp_path):
    (tmp_path / "drawing_context.json").write_text("{broken", encoding="utf-8")
    (tmp_path / "title_block_context.json").write_text("{}", encoding="utf-8")

    result = evaluate_pair("eval_01_format_title", tmp_path)

    assert result["status"] == "FAIL"
    assert any("drawing_context.json" in error for error in result["invalid_artifacts"])


def test_candidate_eval_checks_bbox_shape(tmp_path):
    payload = {
        "candidates": [
            {"candidate_id": "ok", "symbol_bbox": {"x1": 10, "y1": 10, "x2": 30, "y2": 40}},
            {"candidate_id": "bad", "symbol_bbox": {"x1": 20, "y1": 10, "x2": 10, "y2": 40}},
        ]
    }
    (tmp_path / "step5a_candidates_full.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "step5b_associations_full.json").write_text("{}", encoding="utf-8")

    result = evaluate_pair("eval_04_extraction_geometry", tmp_path)

    assert result["status"] == "FAIL"
    assert result["metrics"]["candidate_quality"]["invalid_bounding_boxes"] == 1


def test_model_routing_preserves_operator_override():
    env = {"CDCI_MODEL_EXTRACTION": "gemini-2.5-flash"}
    route = apply_model_routing(env, "7_full_sheet_candidates")

    assert route["model"] == "gemini-2.5-flash"
    assert env["CDCI_MODEL_CLASSIFY"] == "gemini-2.5-flash-lite"


def test_job_store_round_trip_and_events(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    workspace = tmp_path / "job-1"
    store.create("job-1", workspace, "drawing.pdf")
    store.update("job-1", status="running", current_step="format")
    store.event("job-1", "test.event", value=1)

    job = store.get("job-1")

    assert job is not None
    assert job["status"] == "running"
    assert job["payload"]["current_step"] == "format"
    assert job["events"][-1]["event"] == "test.event"


def test_langgraph_contains_conditional_scope_branch_and_join():
    graph = build_graph()
    nodes = set(graph.get_graph().nodes)
    mermaid = graph.get_graph().draw_mermaid()

    assert "cloud_scope_extract_geometry" in nodes
    assert "reuse_full_sheet" in nodes
    assert "build_full_hierarchy" in nodes
    assert "hierarchy_validate_dedup" in nodes
    assert "cloud_scope_extract_geometry" in mermaid
    assert "reuse_full_sheet" in mermaid
