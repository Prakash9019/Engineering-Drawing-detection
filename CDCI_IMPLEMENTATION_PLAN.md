# CDCI Implementation Plan
## Phase-by-Phase Roadmap to Production Agentic AI System

---

## CURRENT STATE (Week 0 — Done)

All 12 pipeline scripts exist and work independently.
Each takes JSON in, produces JSON/Excel out.
No orchestration — you run each command manually.
Result quality: step5a runs (320 candidates) but full pipeline not validated end-to-end.

```
Current: Manual CLI commands → scattered JSON files → no automation
Target:  Agno agentic framework → orchestrated multi-agent system → production API
```

---

## PHASE 1 — Pipeline Hardening & Validation (Week 1-2)

**Goal:** Run the full 12-step pipeline once on 3 real drawings end-to-end and
measure: precision, recall, accuracy against the known ground truth (Annexure-4).

### Step 1.1 — Run full pipeline on your test drawing
```bash
# This is the actual smoke test:
./run_pipeline.sh input_drawing.jpg output/ YOUR_KEY
```

**What to check after each step:**
```
step2:  drawing_context.json has drawing_number, revision_code, is_revision_drawing
step3:  notes_context.json has 20+ notes, rules_prompt_block.txt is non-empty
step4:  sow_symbol_memory.json has allow_count=100, block_count=32
step5a: step5a_candidates.json has 250-350 candidates with valid bboxes
step5b: step5b_associations.json — check spatial_relationship counts
step5c: step5c_validated.json — check PASS/WARN/FAIL ratio
step5d: step5_final_output.json — duplicate reduction 10-30%
step7:  step7_cedm_output.json — check canonical tags match Annexure-4 format
step8:  final_tags.xlsx opens correctly, AUTO_ACCEPT sheet has tags
```

### Step 1.2 — Ground truth comparison
```python
# Compare final_tags.xlsx against ANNEXURE-4 (ground truth)
# Metrics to calculate:
precision = true_positives / (true_positives + false_positives)
recall    = true_positives / (true_positives + false_negatives)
f1_score  = 2 * (precision * recall) / (precision + recall)

# Target (from blueprint §14.3):
# recall    > 95%   (miss < 5% of true tags)
# precision > 90%   (< 10% false positives)
# OCR accuracy > 98% character-level
```

### Step 1.3 — Known gaps to fix before phase 2
```
[ ] step3: Tesseract OCR still has char errors — test against known notes text
[ ] step5a: Some candidates have empty tag_text — improve OCR reconciliation
[ ] step5c: Registry lookup case-sensitivity — normalise before comparison
[ ] step7: Description standardiser — add missing edge cases
[ ] step8: Accept rate likely < 70% first run — tune confidence weights
```

---

## PHASE 2 — run_pipeline.sh + Error Recovery (Week 2-3)

**Goal:** One-command execution with automatic retry and checkpoint resume.

### The orchestrator shell script
```bash
# run_pipeline.sh — what we will build:
./run_pipeline.sh drawing.jpg output/ $KEY

# Features needed:
# 1. Checks prerequisites (API key, Excel files present)
# 2. Runs each step in order
# 3. Validates output JSON exists before running next step
# 4. If a step fails: retries once, then skips with warning
# 5. Checkpoint: if output already exists, skip that step (resume)
# 6. Final summary: counts, rates, output files
```

### Implementation
```bash
#!/bin/bash
set -euo pipefail

DRAWING=$1; OUT=$2; KEY=$3

checkpoint() { [ -f "$OUT/$1" ] && echo "SKIP $1 (exists)" && return 0; return 1; }

checkpoint drawing_context.json || python stages/step1_format_detect.py "$DRAWING" --out "$OUT" --api-key "$KEY"
checkpoint title_block_context.json || python stages/step2_title_block.py --context "$OUT/drawing_context.json" --api-key "$KEY"
checkpoint notes_context.json || python stages/step3_notes_agent.py --context "$OUT/drawing_context.json" --api-key "$KEY"
checkpoint sow_symbol_memory.json || python stages/step4_sow_agent.py build --excel $SOW_EXCEL --out "$OUT" --api-key "$KEY" --skip-vision
checkpoint master_tags.json || python stages/step6_table_agent.py --context "$OUT/drawing_context.json" --api-key "$KEY"
checkpoint step5a_candidates.json || python stages/step5a_candidate_extraction.py --context "$OUT/drawing_context.json" --api-key "$KEY" --workers 8
checkpoint step5b_associations.json || python stages/step5b_geometric_association.py --candidates "$OUT/step5a_candidates.json" --image "$DRAWING" --out "$OUT"
checkpoint step5c_validated.json || python stages/step5c_validation_engine.py --associations "$OUT/step5b_associations.json" --register $REG_EXCEL --notes "$OUT/notes_context.json" --out "$OUT"
checkpoint step5_final_output.json || python stages/step5d_duplicate_resolution.py --validated "$OUT/step5c_validated.json" --out "$OUT"
checkpoint step7_cedm_output.json || python stages/step7_cedm_normalizer.py --final "$OUT/step5_final_output.json" --context "$OUT/drawing_context.json" --out "$OUT"
checkpoint final_tags.xlsx || python stages/step8_confidence_router.py --cedm "$OUT/step7_cedm_output.json" --context "$OUT/drawing_context.json" --out "$OUT"
python stages/step5_visualizer.py --candidates "$OUT/step5a_candidates.json" --deduped "$OUT/step5d_deduped.json" --image "$DRAWING" --out "$OUT"

echo "DONE → $OUT/final_tags.xlsx"
```

---

## PHASE 3 — Agno Agentic Framework Integration (Week 3-5)

**Goal:** Replace the shell script orchestrator with Agno agents.
Each step becomes an Agno Tool. An Orchestrator Agent decides what to run,
handles failures, and manages state.

### What Agno gives us over shell scripts
```
Shell script:     sequential, no reasoning, no self-correction
Agno agents:      can reason about failures, retry with different params,
                  ask for human input when stuck, parallelize intelligently,
                  log every decision with full traceability
```

### Architecture: 5 Agno agents

```
┌──────────────────────────────────────────────────────────────┐
│  ORCHESTRATOR AGENT (Gemini 2.5 Pro)                         │
│  Controls overall pipeline flow                               │
│  Reads drawing_context.json at each step                      │
│  Decides: skip? retry? escalate to human?                    │
└────────────────┬─────────────────────────────────────────────┘
                 │ spawns
     ┌───────────┼───────────┬───────────┐
     ▼           ▼           ▼           ▼
┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
│CONTEXT  │ │EXTRACT  │ │VALIDATE │ │OUTPUT   │
│AGENT    │ │AGENT    │ │AGENT    │ │AGENT    │
│step1-4,6│ │step5a-d │ │step5c,7 │ │step8    │
│parallel │ │parallel │ │serial   │ │serial   │
│tools    │ │workers  │ │tools    │ │tools    │
└─────────┘ └─────────┘ └─────────┘ └─────────┘
```

### Agno tool wrapping (one per step)

```python
# Each existing step becomes an Agno Tool:
from agno.tools import tool

@tool(name="format_detect", description="Detect drawing format and rasterize")
def format_detect_tool(drawing_path: str, out_dir: str, api_key: str) -> dict:
    from step1_format_detect import detect_format_and_parse
    return detect_format_and_parse(drawing_path, out_dir, api_key)

@tool(name="extract_title_block", description="Extract title block and revision intelligence")
def title_block_tool(img_path: str, out_dir: str, api_key: str) -> dict:
    from step2_title_block import run_title_block_extraction
    return run_title_block_extraction(img_path, out_dir, api_key)

# ... one tool per step (12 total)
```

### Orchestrator agent prompt

```python
from agno.agent import Agent
from agno.models.google import Gemini

orchestrator = Agent(
    name="CDCI Pipeline Orchestrator",
    model=Gemini(id="gemini-2.5-pro"),
    tools=[
        format_detect_tool, title_block_tool, notes_extraction_tool,
        sow_scope_tool, table_extraction_tool,
        candidate_extraction_tool, geometric_association_tool,
        validation_tool, dedup_tool,
        cedm_normalizer_tool, confidence_router_tool, visualizer_tool,
    ],
    instructions="""
    You are the CDCI P&ID Tag Extraction Orchestrator.

    Your job is to extract all engineering tags from a P&ID drawing by
    running the 12-step extraction pipeline.

    Rules:
    1. Always run steps in order: 1 → 2 → 3 → 4 → 6 → 5A → 5B → 5C → 5D → 7 → 8
    2. After each step, check the output JSON for errors or warnings
    3. If a step fails: retry once with debug=True, then report failure
    4. If title block says revision_mode=CLOUD_SCOPE_MODE: confirm cloud_detector ran
    5. If accept_rate < 50%: flag for human review before finalising
    6. Always run the visualizer last so humans can review bbox images

    Report: total tags extracted, accept rate, review count, output file path.
    """,
)
```

### Parallel extraction agent

```python
# The extraction agent runs step5a patches in parallel using Agno's
# built-in async execution and reports progress
extraction_agent = Agent(
    name="CDCI Extraction Worker",
    model=Gemini(id="gemini-2.5-pro"),
    tools=[candidate_extraction_tool],
    instructions="""
    Process SAHI patches in parallel. Report progress every 10 patches.
    If a patch fails: log the error and continue with remaining patches.
    Return total candidates found across all patches.
    """,
)
```

---

## PHASE 4 — FastAPI Service Layer (Week 5-6)

**Goal:** Wrap the Agno pipeline as a REST API so it can be called from
a frontend or other services.

```python
# api.py — FastAPI wrapper around the Agno orchestrator
from fastapi import FastAPI, BackgroundTasks, UploadFile
from agno.agent import Agent

app = FastAPI(title="CDCI Tag Extraction API")

@app.post("/extract")
async def extract_drawing(
    file: UploadFile,
    background_tasks: BackgroundTasks,
    project_id: str = "CDCI",
):
    # Save file, start Agno pipeline in background
    drawing_path = save_upload(file)
    task_id = create_task(drawing_path, project_id)
    background_tasks.add_task(run_pipeline, task_id, drawing_path)
    return {"task_id": task_id, "status": "processing"}

@app.get("/status/{task_id}")
async def get_status(task_id: str):
    return get_task_status(task_id)   # reads drawing_context.json

@app.get("/results/{task_id}")
async def get_results(task_id: str):
    return get_task_results(task_id)  # returns final_tags.xlsx + summary
```

---

## PHASE 5 — Production Hardening (Week 6-8)

```
[ ] Add Redis task queue (Celery) for async processing
[ ] PostgreSQL for tag storage (maps to blueprint §12)
[ ] Active learning feedback loop: corrections → retrain
[ ] Multi-drawing batch mode: process folder of drawings
[ ] Context caching: upload drawing to Gemini cache once, reuse all calls
[ ] Cost tracking: log tokens per drawing, alert if > budget threshold
[ ] Monitoring: Prometheus metrics per step (latency, error rate, confidence)
[ ] Docker: containerize all 12 steps + Agno framework
```

---

## IMMEDIATE NEXT ACTIONS (This Week)

```
1. Run full pipeline on input_drawing.jpg and check final_tags.xlsx
2. Compare output against ANNEXURE-4 ground truth — measure precision/recall
3. Fix the top-3 issues from Step 1.3 list
4. Write run_pipeline.sh (Phase 2) — 1-2 hours work
5. pip install agno and test wrapping step1 as an Agno tool
```

---

## REFINEMENT CHECKLIST (Before Agno)

Each step should pass this before adding the framework:

```
step1:  [ ] Test on PDF input    [ ] Test on TIFF input
step2:  [ ] Test on drawing with no revision notice  [ ] Test Rev=0 routing
step3:  [ ] Verify all 25 notes extracted  [ ] Verify abbreviations dict
step4:  [ ] Test classify() on every tag in Annexure-4
step5a: [ ] Accept rate vs expected tag count
step5b: [ ] Verify pipe connections shown in debug image
step5c: [ ] All Annexure-4 tags score PASS
step5d: [ ] Duplicate rate matches SAHI overlap (10-25%)
step6:  [ ] All table rows extracted from TAG LIST
step7:  [ ] Canonical tags match Annexure-4 TAG NUMBER column exactly
step8:  [ ] Accept rate ≥ 70%   [ ] Excel opens cleanly in Excel 365
```

