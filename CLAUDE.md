# CDCI P&ID Tag Extraction Pipeline
## Project Instructions for Claude Code

> **Detailed Architecture & History:** [`PROJECT_MEMORY.md`](PROJECT_MEMORY.md)  
> **Context & Token Optimization:** [`CONTEXT.md`](CONTEXT.md)  
> **Pipeline Runbook:** [`PIPELINE_RUNBOOK.md`](PIPELINE_RUNBOOK.md)

---

## 1. Project Purpose & Scope

Extracts engineering instrument, piping, and equipment tags from scanned P&ID drawings to produce a verified **Annexure-4 Excel tag register** (`output/final_tags.xlsx`) and an engineering hierarchy model (`output/final_hierarchy.xlsx`).

- **Shared State**: `output/drawing_context.json` is the pipeline spine. Never delete or corrupt mid-pipeline.
- **Two-Track Architecture**:
  * **Hierarchy Track**: Requires `--force-full-drawing` on Step 5a → feeds Step 5b2 connectivity and Step 7 `PARENT_EQUIP`.
  * **Deliverable Track**: Cloud-scoped extraction producing revision deliverables.
  * *Gate*: Do not collapse tracks without 50–100 drawing validation (`stages/validate_full_vs_cloud.py`).

---

## 2. Active Scripts

| Phase | Step | Script | Purpose |
|---|---|---|---|
| Context | 1 | `stages/step1_format_detect.py` | Format, size, and layout analysis |
| Context | 2 | `stages/step2_title_block.py` | Title block OCR & drawing metadata |
| Context | 2B | `stages/step2b_cloud_detection.py` | Revision cloud polygon recovery |
| Context | 2C | `step2c_cloud_editor/step2c_cloud_editor.py` | Optional human review of cloud boundaries |
| Context | 3 | `stages/step3_notes_agent.py` | Notes text OCR & parsing |
| Context | 4 | `stages/step4_sow_agent.py` | SOW symbol memory builder (`--skip-vision`) |
| Context | 6 | `stages/step6_table_agent.py` | Equipment table schedule extraction |
| Extraction | **5A** | `stages/step5a_candidate_extraction.py` | SAHI 768px tiling + Gemini detection |
| Validation | 5B | `stages/step5b_geometric_association.py` | Symbol-to-tag geometric association |
| Validation | 5B2 | `stages/step5b2_hierarchy.py` | Process hierarchy, flow, control loops |
| Validation | 5C | `stages/step5c_validation_engine.py` | Deterministic ISA-5.1 validation |
| Validation | 5D | `stages/step5d_duplicate_resolution.py` | Spatial dedup & fragment merging |
| Deliverables | 7 | `stages/step7_cedm_normalizer.py` | CEDM 15-field normalization |
| Deliverables | 8 | `stages/step8_confidence_router.py` | Multi-factor confidence routing |
| Deliverables | 9 | `stages/step9_hierarchy_deliverables.py` | Hierarchy Excel, tree viewer, force graph |

**Prohibited**: `step5a_live_annotator.py`, root-level step3 copies.

---

## 3. Agents & Skills

- **Specialized Agents** (`.claude/agents/`):
  * `architect`: Pipeline contracts, schema boundaries, and model selection.
  * `implementer`: Surgical coding, typed Python 3.10+, minimal diffs.
  * `reviewer`: ISA-5.1 compliance, security scanning, and memory efficiency.
  * `pipeline-debugger`: Stage-by-stage diagnostics, visual overlay inspection.
  * `verifier`: Offline unit testing, recall benchmarks, deliverable audit.
- **Skills** (`.claude/skills/`):
  * `research-first`, `implementation-planning`, `context-optimization`, `project-memory`, `pipeline-debugging`, `testing-and-verification`.
  * Integrates with pre-installed **Superpowers** (`systematic-debugging`, `writing-plans`, `executing-plans`, `test-driven-development`, `verification-before-completion`).

---

## 4. Execution Commands

```bash
# Set environment
export GEMINI_KEY="<YOUR_GEMINI_API_KEY>"
export DRAWING="input_drawing.jpg"

# 1. Fast Offline Unit Tests (<2s)
python -m pytest tests/test_core.py -v

# 2. Syntax & Secret Verification Hooks
python .claude/hooks/code_quality.py
python .claude/hooks/secret_scanner.py

# 3. Ground Truth Recall Evaluation
python stages/eval_coverage.py \
  --candidates output/step5a_candidates.json \
  --register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx \
  --image $DRAWING --out output/

# 4. Excel Deliverable Comparison
python stages/compare_final_vs_annexure4.py
```

---

## 5. Engineering Invariants

1. **No Hardcoded Secrets**: Always read `GEMINI_KEY` from environment variables.
2. **Deterministic Confidence**:
   $$C_{\text{final}} = 0.30 C_{\text{det}} + 0.30 C_{\text{text}} + 0.15 C_{\text{geo}} + 0.20 C_{\text{val}} + 0.05 C_{\text{reg}}$$
   $$\ge 0.80 \rightarrow \text{AUTO\_ACCEPT}, \quad 0.55\text{--}0.80 \rightarrow \text{HUMAN\_REVIEW}, \quad < 0.55 \rightarrow \text{AUTO\_REJECT}$$
3. **Hierarchy Sync**: Re-run `step5b2` whenever `step5a`/`step5b` re-run to avoid candidate ID mismatch.
