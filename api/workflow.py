"""LangGraph orchestration for the existing CDCI stage scripts.

The graph wraps deterministic/model-assisted Python stages in isolated
subprocesses. Full-sheet hierarchy and revision deliverable extraction run on
separate DAG branches and rejoin before validation and final normalization.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any, TypedDict
import operator

from langgraph.graph import END, START, StateGraph

from api.evals import evaluate_pair
from api.model_routing import apply_model_routing
from api.observability import append_jsonl, emit_event


class WorkflowState(TypedDict, total=False):
    job_id: str
    workspace: str
    drawing_path: str
    register_path: str
    sow_path: str
    extraction_scope: str
    workers: int
    status: str
    error: str
    pair_evals: Annotated[list[dict[str, Any]], operator.add]
    artifacts: list[str]


def _paths(state: WorkflowState) -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[1]
    return root, Path(state["workspace"]).resolve()


def _event(state: WorkflowState, event: str, **fields: Any) -> None:
    record = emit_event(event, job_id=state["job_id"], **fields)
    _, out = _paths(state)
    append_jsonl(out / "events.jsonl", record)


def _run(state: WorkflowState, stage: str, script: str, args: list[str],
         required: list[str] | None = None) -> None:
    root, out = _paths(state)
    command = [sys.executable, str(root / script), *args]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    api_key = (env.get("GEMINI_KEY") or env.get("GOOGLE_API_KEY")
               or env.get("GEMINI_API_KEY"))
    if not api_key:
        raise RuntimeError("Set GEMINI_KEY, GOOGLE_API_KEY, or GEMINI_API_KEY on the API worker.")
    env["GOOGLE_API_KEY"] = api_key
    env["GEMINI_API_KEY"] = api_key
    env["GEMINI_KEY"] = api_key
    route = apply_model_routing(env, stage)
    safe_command = list(command)
    if "--api-key" in safe_command:
        key_index = safe_command.index("--api-key") + 1
        if key_index < len(safe_command):
            safe_command[key_index] = "***REDACTED***"

    started = time.perf_counter()
    _event(state, "stage.started", stage=stage, command=safe_command, model_route=route)
    log_dir = out / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    completed = None
    for attempt in range(1, 3):
        try:
            completed = subprocess.run(
                command, cwd=str(root), env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=7200,
            )
        except subprocess.TimeoutExpired as exc:
            if attempt == 1:
                _event(state, "stage.retry", stage=stage, attempt=attempt + 1,
                       reason="subprocess_timeout", delay_seconds=5)
                time.sleep(5)
                continue
            raise RuntimeError(f"{stage} timed out after two attempts") from exc

        if completed.returncode == 0:
            break
        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        transient = any(token in output.lower() for token in (
            "429", "resource_exhausted", "unavailable", "503",
            "temporarily unavailable", "rate limit",
        ))
        if attempt == 1 and transient:
            _event(state, "stage.retry", stage=stage, attempt=attempt + 1,
                   reason="transient_provider_error", delay_seconds=10)
            time.sleep(10)
            continue
        break

    elapsed = round(time.perf_counter() - started, 3)
    with (log_dir / "pipeline.log").open("a", encoding="utf-8") as stream:
        stream.write(f"\n\n=== {stage} ({elapsed}s) ===\n")
        if completed is not None:
            stream.write(completed.stdout or "")
            stream.write(completed.stderr or "")
    exit_code = completed.returncode if completed is not None else -1
    _event(state, "stage.finished", stage=stage,
           duration_seconds=elapsed, exit_code=exit_code, attempts=attempt)
    if completed is None or completed.returncode != 0:
        raise RuntimeError(
            f"{stage} failed with exit code {exit_code}. "
            f"See {log_dir / 'pipeline.log'}"
        )
    for name in required or []:
        if not (out / name).is_file():
            raise FileNotFoundError(f"{stage} did not create required artifact: {name}")


def _step(state: WorkflowState, label: str, script: str, args: list[str],
          required: list[str] | None = None) -> None:
    _event(state, "logical_step.started", step=label)
    _run(state, label, script, args, required)
    _event(state, "logical_step.completed", step=label)


def _eval(state: WorkflowState, eval_id: str) -> list[dict[str, Any]]:
    _, out = _paths(state)
    result = evaluate_pair(eval_id, out)
    _event(state, "eval.completed", **result)
    if result["status"] != "PASS":
        raise RuntimeError(
            f"{eval_id} failed: missing={result['missing_artifacts']}, "
            f"invalid={result['invalid_artifacts']}"
        )
    return [result]


def _pair_01(state: WorkflowState) -> WorkflowState:
    _, out = _paths(state)
    _step(state, "1_format_detection", "stages/step1_format_detect.py",
          [state["drawing_path"], "--out", str(out), "--api-key", (os.getenv("GEMINI_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "")],
          ["drawing_context.json"])
    _step(state, "2_title_revision", "stages/step2_title_block.py",
          ["--context", str(out / "drawing_context.json"), "--api-key", (os.getenv("GEMINI_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "")],
          ["title_block_context.json"])
    return {"pair_evals": _eval(state, "eval_01_format_title")}


def _pair_02(state: WorkflowState) -> WorkflowState:
    _, out = _paths(state)
    _step(state, "3_revision_clouds", "stages/step2b_cloud_detection.py",
          [state["drawing_path"], "--out", str(out), "--api-key", (os.getenv("GEMINI_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "")],
          ["outer_clouds_v2.json"])
    _step(state, "4_notes_context", "stages/step3_notes_agent.py",
          ["--context", str(out / "drawing_context.json"), "--api-key", (os.getenv("GEMINI_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "")],
          ["notes_context.json"])
    return {"pair_evals": _eval(state, "eval_02_cloud_notes")}


def _pair_03(state: WorkflowState) -> WorkflowState:
    _, out = _paths(state)
    _step(state, "5_scope_memory", "stages/step4_sow_agent.py",
          ["build", "--excel", state["sow_path"], "--out", str(out),
           "--skip-vision", "--context", str(out / "drawing_context.json")],
          ["sow_symbol_memory.json"])
    _step(state, "6_table_extraction", "stages/step6_table_agent.py",
          ["--context", str(out / "drawing_context.json"), "--api-key", (os.getenv("GEMINI_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "")],
          ["tables_context.json"])
    return {"pair_evals": _eval(state, "eval_03_scope_tables")}


def _extract_full_sheet(state: WorkflowState) -> WorkflowState:
    _, out = _paths(state)
    workers = max(1, min(int(state.get("workers", 1)), 8))
    common = ["--context", str(out / "drawing_context.json"),
              "--workers", str(workers), "--out", str(out)]
    _step(state, "7_full_sheet_candidates", "stages/step5a_candidate_extraction.py",
          [*common, "--force-full-drawing"], ["step5a_candidates.json"])
    shutil.copy2(out / "step5a_candidates.json", out / "step5a_candidates_full.json")
    _step(state, "8_full_sheet_geometry", "stages/step5b_geometric_association.py",
          ["--candidates", str(out / "step5a_candidates_full.json"),
           "--image", state["drawing_path"], "--out", str(out)],
          ["step5b_associations.json"])
    shutil.copy2(out / "step5b_associations.json", out / "step5b_associations_full.json")
    return {"pair_evals": _eval(state, "eval_04_extraction_geometry")}


def _route_scope(state: WorkflowState) -> WorkflowState:
    _, out = _paths(state)
    context = json.loads((out / "drawing_context.json").read_text(encoding="utf-8"))
    scope = str(context.get("extraction_scope", "FULL_DRAWING")).upper()
    _event(state, "scope.routed", extraction_scope=scope,
           route="cloud_scope" if scope == "CLOUD_ONLY" else "reuse_full_sheet")
    return {"extraction_scope": scope}


def _scope_route(state: WorkflowState) -> str:
    return "cloud" if state.get("extraction_scope") == "CLOUD_ONLY" else "full"


def _cloud_scope_deliverable(state: WorkflowState) -> WorkflowState:
    _, out = _paths(state)
    workers = max(1, min(int(state.get("workers", 1)), 8))
    common = ["--context", str(out / "drawing_context.json"),
              "--workers", str(workers), "--out", str(out)]
    _step(state, "7b_cloud_scope_candidates", "stages/step5a_candidate_extraction.py",
          common, ["step5a_candidates.json"])
    _step(state, "8b_cloud_scope_geometry", "stages/step5b_geometric_association.py",
          ["--candidates", str(out / "step5a_candidates.json"),
           "--image", state["drawing_path"], "--out", str(out)],
          ["step5b_associations.json"])
    return {}


def _reuse_full_sheet_deliverable(state: WorkflowState) -> WorkflowState:
    _, out = _paths(state)
    shutil.copy2(out / "step5a_candidates_full.json", out / "step5a_candidates.json")
    shutil.copy2(out / "step5b_associations_full.json", out / "step5b_associations.json")
    _event(state, "scope.reused_full_sheet", explanation="No cloud-only deliverable filter requested")
    return {}


def _build_full_hierarchy(state: WorkflowState) -> WorkflowState:
    _, out = _paths(state)
    _step(state, "9_hierarchy_graph", "stages/step5b2_hierarchy.py",
          ["--associations", str(out / "step5b_associations_full.json"),
           "--image", state["drawing_path"], "--out", str(out)],
          ["step5b2_hierarchy_full.json"])
    _step(state, "9b_hierarchy_deliverables", "stages/step9_hierarchy_deliverables.py",
          ["--hierarchy", str(out / "step5b2_hierarchy_full.json"),
           "--context", str(out / "drawing_context.json"), "--out", str(out)],
          ["final_hierarchy.xlsx", "hierarchy_viewer.html", "hierarchy_graph.html"])
    return {}


def _deliverable_ready(state: WorkflowState) -> WorkflowState:
    _event(state, "scope.deliverable_ready",
           extraction_scope=state.get("extraction_scope", "FULL_DRAWING"))
    return {}


def _pair_05(state: WorkflowState) -> WorkflowState:
    _, out = _paths(state)
    _step(state, "10_validation", "stages/step5c_validation_engine.py",
          ["--associations", str(out / "step5b_associations.json"),
           "--register", state["register_path"],
           "--notes", str(out / "notes_context.json"), "--out", str(out)],
          ["step5c_validated.json"])
    _step(state, "10b_deduplication", "stages/step5d_duplicate_resolution.py",
          ["--validated", str(out / "step5c_validated.json"), "--out", str(out)],
          ["step5d_deduped.json", "step5_final_output.json"])
    return {"pair_evals": _eval(state, "eval_05_hierarchy_validation")}


def _pair_06(state: WorkflowState) -> WorkflowState:
    _, out = _paths(state)
    _step(state, "11_cedm_normalization", "stages/step7_cedm_normalizer.py",
          ["--final", str(out / "step5_final_output.json"),
           "--context", str(out / "drawing_context.json"), "--out", str(out),
           "--project", "CDCI", "--hierarchy", str(out / "step5b2_hierarchy_full.json")],
          ["step7_cedm_output.json"])
    _step(state, "12_confidence_routing", "stages/step8_confidence_router.py",
          ["--cedm", str(out / "step7_cedm_output.json"),
           "--context", str(out / "drawing_context.json"), "--out", str(out)],
          ["final_tags.xlsx", "human_review_queue.json", "audit_log.json"])
    _step(state, "12b_reporting", "stages/eval_coverage.py",
          ["--candidates", str(out / "step5a_candidates.json"),
           "--register", state["register_path"], "--image", state["drawing_path"],
           "--out", str(out)], ["eval_coverage_report.json"])
    _step(state, "12c_comparison", "stages/compare_final_vs_annexure4.py",
          ["--final", str(out / "final_tags.xlsx"), "--register", state["register_path"],
           "--out", str(out / "final_tags_vs_annexure4.xlsx")],
          ["final_tags_vs_annexure4.xlsx"])
    _step(state, "12d_visualization", "stages/stage_visualizer.py",
          ["--candidates", str(out / "step5a_candidates.json"),
           "--deduped", str(out / "step5d_deduped.json"),
           "--sow", str(out / "sow_symbol_memory.json"),
           "--image", state["drawing_path"], "--out", str(out / "stages")])
    return {"pair_evals": _eval(state, "eval_06_normalization_routing")}


def build_graph(checkpointer: Any = None):
    builder = StateGraph(WorkflowState)
    builder.add_node("format_and_title", _pair_01)
    builder.add_node("clouds_and_notes", _pair_02)
    builder.add_node("scope_and_tables", _pair_03)
    builder.add_node("extract_full_sheet", _extract_full_sheet)
    builder.add_node("route_deliverable_scope", _route_scope)
    builder.add_node("cloud_scope_extract_geometry", _cloud_scope_deliverable)
    builder.add_node("reuse_full_sheet", _reuse_full_sheet_deliverable)
    builder.add_node("deliverable_ready", _deliverable_ready)
    builder.add_node("build_full_hierarchy", _build_full_hierarchy)
    builder.add_node("hierarchy_validate_dedup", _pair_05)
    builder.add_node("normalize_and_route", _pair_06)

    builder.add_edge(START, "format_and_title")
    builder.add_edge("format_and_title", "clouds_and_notes")
    builder.add_edge("clouds_and_notes", "scope_and_tables")
    builder.add_edge("scope_and_tables", "extract_full_sheet")

    # Fan out: the hierarchy always uses full-sheet candidates while the
    # deliverable branch follows the scope decision from drawing metadata.
    builder.add_edge("extract_full_sheet", "build_full_hierarchy")
    builder.add_edge("extract_full_sheet", "route_deliverable_scope")
    builder.add_conditional_edges(
        "route_deliverable_scope",
        _scope_route,
        {"cloud": "cloud_scope_extract_geometry", "full": "reuse_full_sheet"},
    )
    builder.add_edge("cloud_scope_extract_geometry", "deliverable_ready")
    builder.add_edge("reuse_full_sheet", "deliverable_ready")
    builder.add_edge(["build_full_hierarchy", "deliverable_ready"], "hierarchy_validate_dedup")
    builder.add_edge("hierarchy_validate_dedup", "normalize_and_route")
    builder.add_edge("normalize_and_route", END)
    return builder.compile(checkpointer=checkpointer)
