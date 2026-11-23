# Engineering Drawing Detection & P&ID Extraction Pipeline
## Agent Guidance for OpenAI Codex & AI Engineering

This file provides system architecture, development lifecycle, active scripts, and agent guidelines for OpenAI Codex.

---

## 1. System Overview

An automated engineering pipeline that extracts instrument, piping, and equipment tags from scanned P&ID drawings and compiles a normalized tag register in **Annexure-4 Excel format**.

- **Inputs**: `input_drawing.jpg` (or PDF), `ANNEXURE-2_*.xlsx` (SOW), `ANNEXURE-4_*.xlsx` (ground truth)
- **Spine**: `output/drawing_context.json` (shared state across all stages)
- **Outputs**: `output/final_tags.xlsx` (client deliverable), `output/final_hierarchy.xlsx` (engineering hierarchy)
- **Core Stack**: Python 3.10+, OpenCV, Tesseract OCR, Gemini API (`gemini-3.1-pro-preview`, `gemini-2.5-flash`), NumPy, openpyxl, pytest

---

## 2. Active Scripts & Pipeline Execution Order

| Step | Script | Execution Purpose | API Required |
|------|--------|-------------------|--------------|
| 1 | `stages/step1_format_detect.py` | Detect image format, resolution, layout | Optional |
| 2 | `stages/step2_title_block.py` | Title block OCR and metadata extraction | Yes |
| 2B | `stages/step2b_cloud_detection.py` | Revision cloud polygon localization | Yes |
| 2C | `step2c_cloud_editor/step2c_cloud_editor.py` | Optional human cloud review UI | No |
| 3 | `stages/step3_notes_agent.py` | General notes OCR & extraction | Yes |
| 4 | `stages/step4_sow_agent.py` | Scope-of-Work symbol memory builder | No (`--skip-vision`) |
| 6 | `stages/step6_table_agent.py` | Table & schedule OCR extraction | Yes |
| **5A** | `stages/step5a_candidate_extraction.py` | SAHI 768px tiling + Gemini detection | Yes |
| 5B | `stages/step5b_geometric_association.py` | Tag-to-symbol geometric association | No |
| 5B2 | `stages/step5b2_hierarchy.py` | Connectivity graph, flow, control loops | No (opt fallback) |
| 5C | `stages/step5c_validation_engine.py` | ISA-5.1 validation against notes/tables | No |
| 5D | `stages/step5d_duplicate_resolution.py` | Spatial deduplication & fragment merging | No |
| 7 | `stages/step7_cedm_normalizer.py` | CEDM 15-field normalization | No |
| 8 | `stages/step8_confidence_router.py` | Multi-factor scoring → `final_tags.xlsx` | No |
| 9 | `stages/step9_hierarchy_deliverables.py` | Hierarchy workbook, tree, force graph | No |

**Prohibited Scripts**: Do NOT use `step5a_live_annotator.py` or root-level copies of step scripts.

---

## 3. Two-Track Extraction Invariant (Do NOT Violate)

1. **Hierarchy Track**: Requires **FULL** drawing extraction (`--force-full-drawing` on Step 5a). Produces `step5a_candidates_full.json`, `step5b_associations_full.json`, and `step5b2_hierarchy_full.json`. Instruments inside revision clouds often attach to parent equipment outside the cloud.
2. **Deliverable Track**: Cloud-scoped extraction producing `final_tags.xlsx` for revision scope.
3. **Simplification Gate**: Unifying the two tracks into a single full run with post-filtering requires $\ge 50\text{--}100$ validated drawings with $\ge 99\%$ recall and 0 critical misses (`stages/validate_full_vs_cloud.py`).

---

## 4. Development & Execution Lifecycle

When modifying code or fixing pipeline anomalies, strictly follow this 8-step lifecycle:
1. **Research**: Analyze the problem, trace dependencies in `output/drawing_context.json`, and review existing artifacts.
2. **Plan**: Formulate an implementation plan with explicit scopes, tasks, and rollback gates.
3. **Plan Review**: Verify backward compatibility, memory footprint, and token usage before coding.
4. **Incremental Implementation**: Apply surgical edits in `core/` or `stages/`. Avoid speculative refactoring.
5. **Testing**: Run fast offline unit tests (`python -m pytest tests/test_core.py -v`).
6. **Code Review**: Audit Python syntax (`python .claude/hooks/code_quality.py`) and secret detection (`python .claude/hooks/secret_scanner.py`).
7. **Acceptance Verification**: Verify recall against ground truth (`stages/eval_coverage.py`) and inspect generated deliverables.
8. **Memory Sync**: Update `PROJECT_MEMORY.md` if data contracts, schemas, or architectural baselines change.

---

## 5. Codex Skills (`.agents/skills/`)

- `research-first`: Codebase mapping and empirical reproduction prior to modifications.
- `implementation-planning`: Phased plan development with verification gates.
- `context-optimization`: Progressive reading, token efficiency, and subagent delegation.
- `project-memory`: Maintaining persistent architecture records in `PROJECT_MEMORY.md`.
- `pipeline-debugging`: Isolating pipeline failures using visual overlays and intermediate JSONs.
- `testing-and-verification`: Running Tier 1 unit tests and Tier 3 benchmark evaluations.

---

## 6. Standard Execution Commands

```bash
# 1. Fast Unit Tests (<2s)
python -m pytest tests/test_core.py -v

# 2. Code Quality & Secret Check
python .claude/hooks/code_quality.py
python .claude/hooks/secret_scanner.py

# 3. Recall Benchmark Check
python stages/eval_coverage.py --candidates output/step5a_candidates.json --register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx --image input_drawing.jpg --out output/

# 4. Compare Excel Output vs Ground Truth
python stages/compare_final_vs_annexure4.py

# 5. Visual Hierarchy Verification
python stages/visualize_hierarchy.py --hierarchy output/step5b2_hierarchy_full.json --image input_drawing.jpg --out output/hierarchy_verification.jpg
```
