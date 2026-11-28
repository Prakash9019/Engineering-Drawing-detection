"""Six low-token deterministic eval gates (one per pair of logical stages)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PAIRED_EVALS: dict[str, list[str]] = {
    "eval_01_format_title": ["drawing_context.json", "title_block_context.json"],
    "eval_02_cloud_notes": ["outer_clouds_v2.json", "notes_context.json"],
    "eval_03_scope_tables": ["sow_symbol_memory.json", "tables_context.json"],
    "eval_04_extraction_geometry": ["step5a_candidates_full.json", "step5b_associations_full.json"],
    "eval_05_hierarchy_validation": ["step5b2_hierarchy_full.json", "step5c_validated.json", "step5d_deduped.json"],
    "eval_06_normalization_routing": ["step7_cedm_output.json", "final_tags.xlsx", "human_review_queue.json"],
}


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def evaluate_pair(eval_id: str, output_dir: Path) -> dict[str, Any]:
    """Check artifacts and minimal invariants without making any model calls."""
    if eval_id not in PAIRED_EVALS:
        raise ValueError(f"Unknown eval gate: {eval_id}")
    expected = PAIRED_EVALS[eval_id]
    missing = [name for name in expected if not (output_dir / name).is_file()]
    invalid: list[str] = []
    metrics: dict[str, Any] = {}

    for name in expected:
        path = output_dir / name
        if name in missing or path.suffix.lower() != ".json":
            continue
        try:
            data = _read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            invalid.append(f"{name}: {exc}")
            continue
        if isinstance(data, list):
            metrics[name] = {"records": len(data)}
        elif isinstance(data, dict):
            metrics[name] = {"keys": len(data), "records": _record_count(data)}
        else:
            invalid.append(f"{name}: top-level JSON must be an object or array")

    # Lightweight semantic checks for the artifacts whose schemas are stable.
    candidates = output_dir / "step5a_candidates_full.json"
    if candidates.exists():
        try:
            data = _read_json(candidates)
            rows = data if isinstance(data, list) else (
                data.get("candidates") or data.get("results") or data.get("records") or []
            )
            bad_boxes = 0
            for row in rows if isinstance(rows, list) else []:
                box = row.get("symbol_bbox") or row.get("bbox")
                if box is not None and not _valid_box(box):
                    bad_boxes += 1
            metrics["candidate_quality"] = {
                "candidate_count": len(rows) if isinstance(rows, list) else None,
                "invalid_bounding_boxes": bad_boxes,
            }
            if bad_boxes:
                invalid.append(f"candidate file contains {bad_boxes} invalid bounding boxes")
        except (OSError, json.JSONDecodeError):
            pass

    status = "PASS" if not missing and not invalid else "FAIL"
    return {
        "eval_id": eval_id,
        "status": status,
        "expected_artifacts": expected,
        "missing_artifacts": missing,
        "invalid_artifacts": invalid,
        "metrics": metrics,
        "model_calls": 0,
    }


def _record_count(data: dict[str, Any]) -> int | None:
    for key in ("candidates", "associations", "hierarchy", "records", "tags", "rows"):
        value = data.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def _valid_box(box: Any) -> bool:
    if isinstance(box, dict):
        try:
            x1, y1, x2, y2 = (float(box[k]) for k in ("x1", "y1", "x2", "y2"))
            return x2 > x1 and y2 > y1
        except (KeyError, TypeError, ValueError):
            return False
    if isinstance(box, (list, tuple)) and len(box) == 4:
        try:
            x1, y1, x2, y2 = map(float, box)
            return x2 > x1 and y2 > y1
        except (TypeError, ValueError):
            return False
    return False
